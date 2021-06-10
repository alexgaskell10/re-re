from typing import Dict, Optional, List, Any
import logging
import os
import sys
import time
import wandb

import torch
from torch.nn.modules.linear import Linear
from torch import nn
import torch.nn.functional as F

from allennlp.common.util import sanitize
from allennlp.data import Vocabulary
from allennlp.models.archival import load_archive
from allennlp.models.model import Model
from allennlp.nn import RegularizerApplicator, util
from allennlp.training.metrics import CategoricalAccuracy

from .retriever_embedders import (
    SpacyRetrievalEmbedder, TransformerRetrievalEmbedder
)
from .transformer_binary_qa_model import TransformerBinaryQA
from .utils import safe_log, right_pad, batch_lookup, EPSILON, make_dot


@Model.register("gumbel_softmax_unified")
class GumbelSoftmaxRetrieverReasoner(Model):
    def __init__(self,
        qa_model: Model,
        variant: str,
        vocab: Vocabulary = None,
        # pretrained_model: str = None,
        requires_grad: bool = True,
        transformer_weights_model: str = None,
        num_labels: int = 2,
        predictions_file=None,
        layer_freeze_regexes: List[str] = None,
        regularizer: Optional[RegularizerApplicator] = None,
        topk: int = 5,
        sentence_embedding_method: str = 'mean',
        dataset_reader = None,
    ) -> None:
        super().__init__(qa_model.vocab, regularizer)
        self.qa_model = qa_model
        self.qa_model._loss = nn.CrossEntropyLoss(reduction='none')
        self.qa_vocab = qa_model.vocab
        self.vocab = vocab
        self.dataset_reader = dataset_reader
        self.variant = variant
        self.sentence_embedding_method = sentence_embedding_method
        self.similarity_func = 'inner' #'linear' #'inner'       # TODO: set properly
        self.n_retrievals = 1                # TODO: set properly

        # Rollout params
        self.x = -111
        self.gamma = 1          # TODO
        self.beta = 1           # TODO
        self.n_mc = 5           # TODO
        self.num_rollout_steps = topk
        self.retriever_model = None
        self.run_analysis = False   # TODO
        self.baseline = 'n/a'
        self.training = True
        self._context_embs = None

        self._flag = False

        self.define_modules()
        
    def get_retrieval_distr(self, qr, c, meta=None):
        ''' Compute the probability of retrieving each item given
            the current query+retrieval (i.e. p(zj | zi, y))
        '''
        # Compute embeddings
        e_q = self.get_query_embs(qr)['pooled_output']
        self.retriever_model.eval()
        e_c = self.get_context_embs(c)

        # Compute similarities
        if self.similarity_func == 'inner':
            sim = torch.matmul(e_c, e_q.T).squeeze()
        elif self.similarity_func == 'linear':
            e_c_ = e_c.view(-1, e_c.size(-1))
            e_q_ = e_q.repeat(1, e_c.size(1), 1).view(e_c_.shape)
            x = torch.cat((e_q_, e_c_), dim=1)
            sim = self.proj(x).view(e_c.size(0), -1)
            sim = sim.squeeze() if sim.size(0) == 1 else sim
        else:
            raise NotImplementedError()

        # # Ensure padding receives 0 probability mass
        # retrieval_mask = (c != self.retriever_pad_idx).long().unsqueeze(-1)
        # similarity = torch.where(
        #     retrieval_mask.sum(dim=2).squeeze() == 0, 
        #     torch.tensor(-float("inf")).to(c.device), 
        #     sim,
        # )

        # # Deal with nans- these are caused by all sentences being padding.
        # similarity[torch.isinf(similarity).all(dim=-1)] = 1 / similarity.size(0)
        # if torch.isinf(similarity).all(dim=-1).any():
        #     raise ValueError('All retrievals are -inf for a sample. This will lead to nan loss')

        # return similarity

        return sim

    def answer(self, qr, label, metadata):
        return self.get_query_embs(qr, label, metadata)
        
    def get_query_embs(self, qr, label=None, metadata=None):
        qr_ = {'tokens': {'token_ids': qr, 'type_ids': torch.zeros_like(qr)}}
        return self.qa_model(qr_, label, metadata)

    @torch.no_grad()
    def get_context_embs(self, c):
        return self.retriever_model(c)
        # if self._context_embs is None:
        #     self._context_embs = self.retriever_model(c)
        # return self._context_embs
    
    def forward(self, 
        label: torch.LongTensor = None,
        metadata: List[Dict[str, Any]] = None,
        retrieval: List = None,
        **kwargs,
    ) -> torch.Tensor:
        ''' Rollout the forward pass of the network. Consists of
            n retrieval steps followed by an answering step.
        '''
        qr = retrieval['tokens']['token_ids'][:,0,:]
        c = retrieval['tokens']['token_ids'][:,1:,:]
        _d = qr.device

        print(metadata[0]['id'])
        if 'RelNeg-D5-168-1' == metadata[0]['id']:
            flag = True
        else:
            flag = False

        # TODO: add an "end" context item of a blank one or something....

        # Storage tensors
        policies, actions, unscaled_retrieval_losses = [],[],[]
        
        # Retrieval rollout phase
        for t in range(self.num_rollout_steps):
            policy = self.get_retrieval_distr(qr, c, metadata)
            action = self.gs(policy, tau=1)
            if flag:
                action = torch.zeros_like(action).scatter(0, torch.tensor(16).cuda(), 1)
            if qr.size(0) == 1:
                # To allow bsz = 1...
                policy = policy.unsqueeze(0)
                action = action.unsqueeze(0)
            loss = self.retriever_loss(policy, action.argmax(-1))

            policies.append(policy)
            actions.append(action)
            unscaled_retrieval_losses.append(loss)

            # qr, c, metadata = self.prep_next_batch(c, metadata, actions, t)
            qr, _, metadata = self.prep_next_batch(c, metadata, actions, t)

        # Query answering phase
        self.update_meta(qr, metadata, actions)
        output = self.answer(qr, label, metadata)

        # Scale retrieval losses by final loss
        qa_loss = output['loss'].detach()       # TODO
        unscaled_retrieval_losses_ = torch.cat([u.unsqueeze(0) for u in unscaled_retrieval_losses])
        # retrieval_losses = qa_loss * unscaled_retrieval_losses_       # NOTE: original
        retrieval_losses = unscaled_retrieval_losses_ #* torch.exp(-qa_loss)
        # total_loss = (qa_loss + retrieval_losses / retrieval_losses.size(0))
        total_loss = retrieval_losses
        # output['loss'] = total_loss.mean()
        output['loss'] = loss
        # output['loss'] = unscaled_retrieval_losses.mean()
        
        # Record trajectory data
        output['unnorm_policies'] = policies
        output['sampled_actions'] = torch.cat([a.unsqueeze(0) for a in actions]).argmax(dim=-1)

        self._context_embs = None

        # learning_rate = 1e-4
        # if not hasattr(self, 'optimizer'):
        #     self.optimizer = torch.optim.Adam(self.qa_model.parameters(), lr=learning_rate)
        # self.optimizer.zero_grad()
        # loss.backward()
        # self.optimizer.step()
        # output['loss'] = torch.tensor([0]).cuda()

        return output

    def gs(self, logits, tau=1):
        ''' Gumbel softmax
        '''
        return F.gumbel_softmax(logits, tau=tau, hard=True, eps=1e-10, dim=-1)

    def update_meta(self, query_retrieval, metadata, actions):
        ''' Log relevant metadata for later use.
        '''
        retrievals = torch.cat([a.unsqueeze(0) for a in actions]).argmax(dim=-1).T
        for qr, topk, meta in zip(query_retrieval, retrievals, metadata):
            meta['topk'] = topk.tolist()
            meta['query_retrieval'] = qr.tolist()

    def prep_next_batch(self, context, metadata, actions, t):
        ''' Concatenate the latest retrieval to the current 
            query+retrievals. Also update the tensors for the next
            rollout pass.
        '''
        # Get indexes of retrieval items
        retrievals = torch.cat([a.unsqueeze(0) for a in actions]).argmax(dim=-1).T

        # Concatenate query + retrival to make new query_retrieval
        # tensor of idxs        
        sentences = []
        for topk, meta in zip(retrievals, metadata):
            question = meta['question_text']
            sentence_idxs = [int(i) for i in topk.tolist()[:t+1] if i != self.x]
            context_str = ''.join([
                toks + '.' for n, toks in enumerate(meta['context'].split('.')[:-1]) 
                if n in sentence_idxs
            ]).strip()
            meta['context_str'] = f"q: {question} c: {context_str}"
            sentences.append((question, context_str))
        batch = self.dataset_reader.transformer_indices_from_qa(sentences, self.qa_vocab)
        query_retrieval = batch['phrase']['tokens']['token_ids'].to(context.device)

        # Replace retrieved context with padding so same context isn't retrieved twice
        current_action = actions[t].argmax(dim=1)
        context = context.scatter(
            1, current_action.repeat(context.size(-1), 1, 1).T, self.retriever_pad_idx
        )

        return query_retrieval, context, metadata

    def predict(self):
        pass

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        if reset == True and not self.training:
            return {
                'EM': self.qa_model._accuracy.get_metric(reset),
                'predictions': self.qa_model._predictions,
            }
        else:
            return {
                'EM': self.qa_model._accuracy.get_metric(reset),
            }

    def define_modules(self):
        if self.variant == 'spacy':
            self.retriever_model = SpacyRetrievalEmbedder(
                sentence_embedding_method=self.sentence_embedding_method,
                vocab=self.vocab,
                variant=self.variant,
            )
            self.tok_name = 'tokens'
            self.retriever_pad_idx = self.vocab.get_token_index(self.vocab._padding_token)      # TODO: standardize these
        elif 'roberta' in self.variant:
            self.retriever_model = TransformerRetrievalEmbedder(
                sentence_embedding_method=self.sentence_embedding_method,
                vocab=self.vocab,
                variant=self.variant,
            )
            self.tok_name = 'token_ids'
            self.retriever_pad_idx = self.dataset_reader.pad_idx(mode='retriever')       # TODO: standardize these
        else:
            raise ValueError(
                f"Invalid retriever_variant: {self.variant}.\nInvestigate!"
            )

        if self.similarity_func == 'linear':
            self.proj = nn.Linear(2*self.qa_model._output_dim, 1)      # TODO: sort for different retriever and qa models

        self.retriever_loss = nn.CrossEntropyLoss(reduction='none')

