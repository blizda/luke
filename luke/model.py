import copy
import logging
import torch
import torch.nn.functional as F
from torch import nn
from pytorch_transformers.modeling_bert import BertConfig, BertLayerNorm, BertEmbeddings, BertEncoder, BertPooler,\
    BertPredictionHeadTransform

logger = logging.getLogger(__name__)

EPS = 1e-12


class LukeConfig(BertConfig):
    def __init__(self, vocab_size, entity_vocab_size, **kwargs):
        super(LukeConfig, self).__init__(vocab_size, **kwargs)

        self.entity_vocab_size = entity_vocab_size


class LukeE2EConfig(LukeConfig):
    def __init__(self, num_el_hidden_layers, entity_selector_softmax_temp, **kwargs):
        super(LukeE2EConfig, self).__init__(**kwargs)

        self.num_el_hidden_layers = num_el_hidden_layers
        self.entity_selector_softmax_temp = entity_selector_softmax_temp


class EntityEmbeddings(nn.Module):
    def __init__(self, config, entity_vocab_size=None):
        super(EntityEmbeddings, self).__init__()
        self.config = config
        if entity_vocab_size is None:
            entity_vocab_size = config.entity_vocab_size

        self.entity_embeddings = nn.Embedding(entity_vocab_size, config.hidden_size, padding_idx=0)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size)

        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, entity_ids, position_ids, token_type_ids=None):
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(entity_ids)

        entity_embeddings = self.entity_embeddings(entity_ids)

        position_embeddings = self.position_embeddings(position_ids.clamp(min=0))
        position_embedding_mask = (position_ids != -1).type_as(position_embeddings).unsqueeze(-1)
        position_embeddings = position_embeddings * position_embedding_mask
        position_embeddings = torch.sum(position_embeddings, dim=-2)
        position_embeddings = position_embeddings / (position_embedding_mask.sum(dim=-2) + EPS)

        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = entity_embeddings + position_embeddings + token_type_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)

        return embeddings


class EntitySelector(nn.Module):
    def __init__(self, config):
        super(EntitySelector, self).__init__()
        self.transform = BertPredictionHeadTransform(config)
        self.embeddings = nn.Embedding(config.entity_vocab_size, config.hidden_size, padding_idx=0)
        self.bias = nn.Embedding(config.entity_vocab_size, 1, padding_idx=0)

    def forward(self, hidden_states, entity_candidate_ids):
        entity_embeddings = self.embeddings(entity_candidate_ids)
        entity_bias = self.bias(entity_candidate_ids).squeeze(-1)

        hidden_states = self.transform(hidden_states)

        scores = (hidden_states.unsqueeze(-2) * entity_embeddings).sum(-1) + entity_bias
        scores += (entity_candidate_ids == 0).to(dtype=scores.dtype) * -10000.0

        return scores


class LukeBaseModel(nn.Module):
    def __init__(self, config):
        super(LukeBaseModel, self).__init__()

        self.config = config

        self.encoder = BertEncoder(config)
        self.pooler = BertPooler(config)
        self.embeddings = BertEmbeddings(config)
        self.entity_embeddings = EntityEmbeddings(config)

    def init_weights(self, module):
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, nn.Embedding):
            if module.embedding_dim == 1:  # embedding for bias parameters
                module.weight.data.zero_()
            else:
                module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, BertLayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    def load_bert_weights(self, state_dict):
        state_dict = state_dict.copy()
        for key in list(state_dict.keys()):
            new_key = key.replace('gamma', 'weight').replace('beta', 'bias')
            if new_key.startswith('bert.'):
                new_key = new_key[5:]

            if key != new_key:
                state_dict[new_key] = state_dict[key]
                del state_dict[key]

        missing_keys = []
        unexpected_keys = []
        error_msgs = []

        metadata = getattr(state_dict, '_metadata', None)
        state_dict = state_dict.copy()
        if metadata is not None:
            state_dict._metadata = metadata

        def load(module, prefix=''):
            local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
            module._load_from_state_dict(state_dict, prefix, local_metadata, True, missing_keys, unexpected_keys,
                                         error_msgs)
            for name, child in module._modules.items():
                if child is not None:
                    load(child, prefix + name + '.')

        load(self, prefix='')
        if len(unexpected_keys) > 0:
            logger.info("Weights from pretrained model not used in {}: {}".format(self.__class__.__name__,
                                                                                  sorted(unexpected_keys)))
        if len(error_msgs) > 0:
            raise RuntimeError('Error(s) in loading state_dict for {}:\n\t{}'.format(self.__class__.__name__,
                                                                                     "\n\t".join(error_msgs)))

    def _compute_extended_attention_mask(self, word_attention_mask, entity_attention_mask):
        attention_mask = torch.cat([word_attention_mask, entity_attention_mask], dim=1)
        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        return extended_attention_mask


class LukeModel(LukeBaseModel):
    def forward(self, word_ids, word_segment_ids, word_attention_mask, entity_ids, entity_position_ids,
                entity_segment_ids, entity_attention_mask):
        word_seq_size = word_ids.size(1)
        extended_attention_mask = self._compute_extended_attention_mask(word_attention_mask, entity_attention_mask)

        word_embedding_output = self.embeddings(word_ids, word_segment_ids)
        entity_embedding_output = self.entity_embeddings(entity_ids, entity_position_ids, entity_segment_ids)
        embedding_output = torch.cat([word_embedding_output, entity_embedding_output], dim=1)
        encoder_outputs = self.encoder(embedding_output, extended_attention_mask,
                                       [None] * self.config.num_hidden_layers)
        sequence_output = encoder_outputs[0]
        word_sequence_output = sequence_output[:, :word_seq_size, :]
        entity_sequence_output = sequence_output[:, word_seq_size:, :]
        pooled_output = self.pooler(sequence_output)

        return (word_sequence_output, entity_sequence_output, pooled_output,) + encoder_outputs[1:]


class LukeE2EModel(LukeBaseModel):
    def __init__(self, config):
        super(LukeE2EModel, self).__init__(config)

        self.mask_entity_embeddings = EntityEmbeddings(config, 2)
        el_config = copy.copy(config)
        el_config.num_hidden_layers = config.num_el_hidden_layers
        self.el_encoder = BertEncoder(config)
        self.entity_selector = EntitySelector(config)
        self.entity_selector.embeddings.weight = self.entity_embeddings.entity_embeddings.weight

    def forward(self, word_ids, word_segment_ids, word_attention_mask, entity_candidate_ids, entity_position_ids,
                entity_segment_ids, entity_attention_mask, masked_entity_labels=None,
                output_entity_selector_scores=False):
        word_seq_size = word_ids.size(1)
        extended_attention_mask = self._compute_extended_attention_mask(word_attention_mask, entity_attention_mask)

        word_embedding_output = self.embeddings(word_ids, word_segment_ids)
        mask_entity_embedding_output = self.mask_entity_embeddings(entity_attention_mask, entity_position_ids,
                                                                   entity_segment_ids)

        el_embedding_output = torch.cat([word_embedding_output, mask_entity_embedding_output], dim=1)
        el_encoder_outputs = self.el_encoder(el_embedding_output, extended_attention_mask,
                                             [None] * self.config.num_el_hidden_layers)
        el_entity_sequence_output = el_encoder_outputs[0][:, word_seq_size:, :]
        entity_selector_scores = self.entity_selector(el_entity_sequence_output, entity_candidate_ids)
        entity_selector_scores = entity_selector_scores / self.config.entity_selector_softmax_temp
        entity_attention_probs = F.softmax(entity_selector_scores, dim=-1)

        entity_embedding_output = self.entity_embeddings(entity_candidate_ids, entity_position_ids.unsqueeze(-2),
                                                         entity_segment_ids.unsqueeze(-1))
        entity_embedding_output = (entity_embedding_output * entity_attention_probs.unsqueeze(-1)).sum(-2)

        if masked_entity_labels is not None:
            mask_entity_ids = entity_segment_ids.new_full(entity_segment_ids.size(), 2)  # index of [MASK] token is 2
            mask_embedding_output = self.entity_embeddings(mask_entity_ids, entity_position_ids, entity_segment_ids)
            mask_embedding_output = mask_embedding_output.masked_fill((masked_entity_labels == -1).unsqueeze(-1), 0)
            entity_embedding_output = entity_embedding_output.masked_fill((masked_entity_labels != -1).unsqueeze(-1), 0)
            entity_embedding_output = entity_embedding_output + mask_embedding_output
        embedding_output = torch.cat([word_embedding_output, entity_embedding_output], dim=1)
        encoder_outputs = self.encoder(embedding_output, extended_attention_mask,
                                       [None] * self.config.num_hidden_layers)
        sequence_output = encoder_outputs[0]
        word_sequence_output = sequence_output[:, :word_seq_size, :]
        entity_sequence_output = sequence_output[:, word_seq_size:, :]
        pooled_output = self.pooler(sequence_output)

        if output_entity_selector_scores:
            return (word_sequence_output, entity_sequence_output, pooled_output, entity_selector_scores) +\
                encoder_outputs[1:]
        else:
            return (word_sequence_output, entity_sequence_output, pooled_output) + encoder_outputs[1:]
