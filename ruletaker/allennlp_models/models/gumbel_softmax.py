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
        self.W = nn.Linear(self.retriever_model.embedder.config.hidden_size, 1)        # TODO
        self.answers = []

    def get_retrieval_distr(self, qr, meta=None):
        ''' Compute the probability of retrieving each item given
            the current query+retrieval (i.e. p(zj | zi, y))
        '''
        e_q = self.get_context_embs(qr)
        sim = self.W(e_q).squeeze(-1)

        # Ensure padding receives 0 probability mass
        similarity = torch.where(
            qr.max(dim=2)[0] == self.retriever_pad_idx,     # Identify rows which contain all padding
            torch.tensor(-float("inf")).to(e_q.device), 
            sim,
        )

        # Deal with nans- these are caused by all sentences being padding.
        similarity[torch.isinf(similarity).all(dim=-1)] = 1 / similarity.size(0)
        if torch.isinf(similarity).all(dim=-1).any():
            raise ValueError('All retrievals are -inf for a sample. This will lead to nan loss')

        return similarity
        return sim

    def answer(self, qr, label, metadata):
        return self.get_query_embs(qr, label, metadata)
        
    def get_query_embs(self, qr, label=None, metadata=None):
        qr_ = {'tokens': {'token_ids': qr, 'type_ids': torch.zeros_like(qr)}}
        return self.qa_model(qr_, label, metadata)

    # @torch.no_grad()
    def get_context_embs(self, c):
        # if self._context_embs is None:
        #     self._context_embs = self.retriever_model(c)
        # return self._context_embs
        return self.retriever_model(c)

    def forward(self, 
        label: torch.LongTensor = None,
        metadata: List[Dict[str, Any]] = None,
        retrieval: List = None,
        **kwargs,
    ) -> torch.Tensor:
        ''' Rollout the forward pass of the network. Consists of
            n retrieval steps followed by an answering step.
        '''
        _qr = retrieval['tokens']['token_ids']
        qr = _qr[:]       # shape = (bsz, context_len, sentence_len)
        _d = qr.device

        flag = False
        self.ms = torch.tensor([m['exact_match'] for m in metadata]).to(_d)
        nl = [torch.tensor(m['node_label'][:-1]).nonzero().squeeze().to(_d) for m in metadata] # [:-1] because final node is NAF node
        naf = [torch.tensor(m['node_label'][-1:]).nonzero().squeeze().to(_d) for m in metadata]

        # TODO: add an "end" context item of a blank one or something....

        # Storage tensors
        policies, actions, unscaled_retrieval_losses = [],[],[]
        
        # Retrieval rollout phase
        for t in range(self.num_rollout_steps):
            policy = self.get_retrieval_distr(qr, metadata)
            action = self.gs(policy, tau=1)
            if flag:
                action = torch.zeros_like(action).scatter(1, torch.tensor([16]*action.size(0)).view(-1,1).cuda(), 1)
            loss = self.retriever_loss(policy, action.argmax(-1))

            policies.append(policy)
            actions.append(action)
            unscaled_retrieval_losses.append(loss)

            q = qr.gather(1, action.argmax(-1).view(-1, 1, 1).repeat(1, 1, qr.size(-1))).squeeze(1)
            qr, metadata = self.prep_next_batch(qr, metadata, actions, t)

            a = action.argmax(-1)
            p = policy.argmax(-1)
            p_ = policy.softmax(-1)
            argmax_ps = p_.gather(1, p.unsqueeze(1)).squeeze()
            action_ps = p_.gather(1, a.unsqueeze(1)).squeeze()

            if False:
                # d=0 checks
                ac, pol, both, e = None, None, None, None
                correct_actions = (self.ms == a).nonzero()
                correct_policy = (self.ms == p).nonzero()
                # self.dataset_reader.decode(q[e])
                # [self.dataset_reader.decode(_qr[e,i]) for i in range(_qr.size(1))]
                # p_[e, self.ms[e]]
                correct_both = ((self.ms == p) & (self.ms == a)).nonzero()
                em = (self.ms != -1).nonzero()
                if correct_actions.numel():
                    ac = correct_actions[0,0]
                    print('A')
                if correct_policy.numel():
                    pol = correct_policy[0,0]
                    print('B')
                if em.numel():
                    e = em[0,0]
                if correct_both.numel():
                    idx = correct_policy[0,0]
                    print('C')

            # d=1 checks
            i = 0
            if a[i] in nl[i]:
                print('A')
            if p[i] in nl[i]:
                print('B')
            if a[i] in nl[i] and p[i] in nl[i]:
                print('C')
            
        # Query answering phase
        self.update_meta(q, metadata, actions)
        output = self.answer(q, label, metadata)

        # Scale retrieval losses by final loss
        qa_loss = output['loss'].detach()
        qa_scale = torch.gather(output['label_probs'].detach(), dim=1, index=label.unsqueeze(1))
        unscaled_retrieval_losses_ = torch.cat([u.unsqueeze(1) for u in unscaled_retrieval_losses], dim=1)      # TODO: check this works for bsz > 1
        retrieval_losses = qa_scale * unscaled_retrieval_losses_ / unscaled_retrieval_losses_.size(1)      # NOTE: originals
        # retrieval_losses = output['label_probs'][:,1].detach() * qa_scale * unscaled_retrieval_losses_ / unscaled_retrieval_losses_.size(0)      # NOTE: originals
        # total_loss = qa_loss + unscaled_retrieval_losses_ #retrieval_losses
        total_loss = retrieval_losses
        output['loss'] = total_loss.mean()
        # output['loss'] = loss
        # output['loss'] = unscaled_retrieval_losses_.mean()

        # # Record trajectory data
        pol = torch.cat([p.unsqueeze(0) for p in policies])
        act = torch.cat([a.unsqueeze(0) for a in actions])
        # output['unnorm_policies'] = policies
        # output['sampled_actions'] = torch.cat([a.unsqueeze(0) for a in actions]).argmax(dim=-1)

        # d=1 further checks
        correct_acts = torch.equal(act.argmax(-1).unique(), nl[i])
        correct_pols = torch.equal(pol.argmax(-1).unique(), nl[i])
        max_probs = [p.softmax(-1)[i,k].item() for k,p in zip(pol.argmax(-1).squeeze().tolist(), policies)]     # Probability of the most likely action given the policy
        act_probs = [p.softmax(-1)[i,k].item() for k,p in zip(act.argmax(-1).squeeze().tolist(), policies)]     # Probability of selecting the action given the policy
        nl_probs = [p.softmax(-1)[i,nl[i]].tolist() for p in policies]      # The policy probabilities of selecting the proof items
        if correct_acts:
            print('AA')
        if correct_pols:
            print('BB')
        if correct_acts and correct_pols:
            print('CC')

        self._context_embs = None

        print(f'\n{qa_loss.mean().item():.3f}   {retrieval_losses.mean().item():.3f}   {(output["label_probs"].argmax(-1) == label).float().mean().item()}')

        # if (self.ms == action.argmax(-1).item()) and (output["label_probs"].argmax(-1).item() == label):
        #     print('C')

        if any(output["label_probs"].argmax(-1) == label):
            print('E')

        self.answers.extend((output["label_probs"].argmax(-1) == label).tolist())
        all_score = self.answers.count(True) / len(self.answers)
        last_100 = self.answers[-100:].count(True) / len(self.answers[-100:])

        print(f'\n\nAll: {all_score}\tLast 100: {last_100}')

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

    def prep_next_batch(self, qr, metadata, actions, t):
        ''' Concatenate the latest retrieval to the current 
            query+retrievals. Also update the tensors for the next
            rollout pass.
        '''
        # Get indexes of retrieval items
        retrievals = torch.cat([a.unsqueeze(0) for a in actions]).argmax(dim=-1).T

        # Concatenate query + retrival to make new query_retrieval
        # matrix of idxs        
        sentences = []
        for topk, meta in zip(retrievals, metadata):
            question = meta['question_text']
            sentence_idxs = [int(i) for i in topk.tolist()[:t+1] if i != self.x]
            context_rtr = [
                toks + '.' for n, toks in enumerate(meta['context'].split('.')[:-1]) 
                if n in sentence_idxs
            ]
            meta['context_str'] = f"q: {question} c: {''.join(context_rtr).strip()}"
            sentences.append((question, ''.join(context_rtr).strip(), meta['context']))

        batch = self.dataset_reader.transformer_indices_from_qa(sentences, self.qa_vocab)
        qr_ = batch['retrieval']['tokens']['token_ids'].to(qr.device)

        # Replace retrieved context with padding so same context isn't retrieved twice
        current_action = actions[t].argmax(dim=1)

        qr_ = qr_.scatter(
            1, current_action.repeat(qr_.size(-1), 1, 1).T, self.retriever_pad_idx
        )

        return qr_, metadata

    def prep_next_batch_1(self, context, metadata, actions, t):
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

        set_dropout(self.retriever_model, 0.0)
        set_dropout(self.qa_model, 0.0)

    def forward_1(self, 
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

        # if 'RelNeg-D5-168-1' == metadata[0]['id']:
        #     flag = True
        # else:
        #     flag = False
        flag = False

        d0_match = [all(qr[0, :c.size(-1)] == c[0,i,:qr.size(-1)]) for i in range(c.size(1))]
        self.d0_match_idx = d0_match.index(True) if any(d0_match) else None

        # TODO: add an "end" context item of a blank one or something....

        # Storage tensors
        policies, actions, unscaled_retrieval_losses = [],[],[]
        
        # Retrieval rollout phase
        for t in range(self.num_rollout_steps):
            if self.d0_match_idx is not None:
                print()
            policy = self.get_retrieval_distr(qr, c, metadata)
            action = self.gs(policy, tau=10)
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

            qr, _, metadata = self.prep_next_batch(c, metadata, actions, t)

        # Query answering phase
        self.update_meta(qr, metadata, actions)
        output = self.answer(qr, label, metadata)

        # Scale retrieval losses by final loss
        qa_loss = output['loss'].detach()
        qa_scale = torch.gather(output['label_probs'].detach(), dim=1, index=label.unsqueeze(1))
        unscaled_retrieval_losses_ = torch.cat([u.unsqueeze(0) for u in unscaled_retrieval_losses])
        retrieval_losses = qa_scale * unscaled_retrieval_losses_ / unscaled_retrieval_losses_.size(0)      # NOTE: originals
        # retrieval_losses = output['label_probs'][:,1].detach() * qa_scale * unscaled_retrieval_losses_ / unscaled_retrieval_losses_.size(0)      # NOTE: originals
        # total_loss = qa_loss + unscaled_retrieval_losses_ #retrieval_losses
        total_loss = retrieval_losses
        output['loss'] = total_loss.mean()
        # output['loss'] = loss
        # output['loss'] = unscaled_retrieval_losses_.mean()
        
        # Record trajectory data
        output['unnorm_policies'] = policies
        output['sampled_actions'] = torch.cat([a.unsqueeze(0) for a in actions]).argmax(dim=-1)

        self._context_embs = None

        print(f'{qa_loss.mean().item():.3f}   {retrieval_losses.mean().item():.3f}   {(output["label_probs"].argmax(-1) == label).float().mean().item()}')

        if self.d0_match_idx == action.argmax(-1).item():
            print('ABC')

        if self.d0_match_idx == policy.argmax(-1).item():
            print('ABC')

        if (self.d0_match_idx == action.argmax(-1).item()) and (output["label_probs"].argmax(-1).item() == label):
            print('ABC')

        return output

    def get_retrieval_distr_1(self, qr, c, meta=None):
        ''' Compute the probability of retrieving each item given
            the current query+retrieval (i.e. p(zj | zi, y))
        '''
        # Compute embeddings
        # e_q = self.get_query_embs(qr)['pooled_output']
        # self.retriever_model.train()
        e_q = self.get_context_embs(qr).detach()
        e_c = self.get_context_embs(c)

        # Compute similarities
        if self.similarity_func == 'inner':
            # x = torch.cat([e_q.unsqueeze(1).repeat(1, e_c.size(1), 1), e_c], dim=2)
            # x = F.relu(self.W1(x))
            # x = F.relu(self.W2(x))
            # sim = self.W3(x).squeeze()
            # sim = self.W(e_c).matmul(e_q.T).squeeze()
            sim = torch.matmul(e_c, e_q.T).squeeze() / e_c.size(-1)**0.5
            # sim = torch.cosine_similarity(
            #     e_c, e_q.unsqueeze(1).repeat(1, e_c.size(1), 1), dim=2
            # ).squeeze()
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



def set_dropout(model, drop_rate=0.1):
    for name, child in model.named_children():
        if isinstance(child, torch.nn.Dropout):
            child.p = drop_rate
        set_dropout(child, drop_rate=drop_rate)
