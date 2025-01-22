from typing import Optional
from itertools import chain
from functools import partial
import pdb
import torch
import torch.nn as nn
import torch
import torch.nn as nn
import torch.nn.functional as F
import dgl
from dgl.ops import edge_softmax
import dgl.function as fn
from dgl.utils import expand_as_pair
import pdb

from models.utils import create_activation, NormLayer, create_norm, drop_edge

from models.loss_func import sce_loss


def setup_module(m_type, enc_dec, in_dim, num_hidden, out_dim, num_layers, dropout, activation, residual, norm, nhead,
                 nhead_out, attn_drop, negative_slope=0.2, concat_out=True) -> nn.Module:
    if m_type == "gat":
        mod = GAT(
            in_dim=in_dim,
            num_hidden=num_hidden,
            out_dim=out_dim,
            num_layers=num_layers,
            nhead=nhead,
            nhead_out=nhead_out,
            concat_out=concat_out,
            activation=activation,
            feat_drop=dropout,
            attn_drop=attn_drop,
            negative_slope=negative_slope,
            residual=residual,
            norm=create_norm(norm),
            encoding=(enc_dec == "encoding"),
        )
    elif m_type == "gin":
        mod = GIN(
            in_dim=in_dim,
            num_hidden=num_hidden,
            out_dim=out_dim,
            num_layers=num_layers,
            dropout=dropout,
            activation=activation,
            residual=residual,
            norm=norm,
            encoding=(enc_dec == "encoding"),
        )
    elif m_type == "gcn":
        mod = GCN(
            in_dim=in_dim,
            num_hidden=num_hidden,
            out_dim=out_dim,
            num_layers=num_layers,
            dropout=dropout,
            activation=activation,
            residual=residual,
            norm=create_norm(norm),
            encoding=(enc_dec == "encoding")
        )
    elif m_type == "mlp":
        # * just for decoder
        mod = nn.Sequential(
            nn.Linear(in_dim, num_hidden),
            nn.PReLU(),
            nn.Dropout(0.2),
            nn.Linear(num_hidden, out_dim)
        )
    elif m_type == "linear":
        mod = nn.Linear(in_dim, out_dim)
    else:
        raise NotImplementedError

    return mod


# create a n layer mlp with residual connection and linear output
class ResMLP(nn.Module):
    def __init__(self, num_layers, input_dim, hidden_dim, output_dim, activation="prelu", norm="batchnorm"):
        super(ResMLP, self).__init__()
        self.linear_or_not = True  # default is linear model
        self.num_layers = num_layers
        self.output_dim = output_dim
        assert input_dim == output_dim
        if num_layers < 1:
            raise ValueError("number of layers should be positive!")
        elif num_layers == 1:
            # Linear model
            self.linear = nn.Linear(input_dim, output_dim)
        else:
            # Multi-layer model
            self.linear_or_not = False
            self.linears = torch.nn.ModuleList()
            self.norms = torch.nn.ModuleList()
            self.activations = torch.nn.ModuleList()

            self.linears.append(nn.Linear(input_dim, hidden_dim))
            for _ in range(num_layers - 2):
                self.linears.append(nn.Linear(hidden_dim, hidden_dim))
            self.linears.append(nn.Linear(hidden_dim, output_dim))

            for _ in range(num_layers - 1):
                self.norms.append(create_norm(norm)(hidden_dim))
                self.activations.append(create_activation(activation))

    def forward(self, x):
        if self.linear_or_not:
            # If linear model
            return self.linear(x)
        else:
            # If MLP
            h = x
            for i in range(self.num_layers - 1):
                h = self.norms[i](self.linears[i](h))
                h = self.activations[i](h)
            return self.linears[-1](h) + x


class GAT(nn.Module):
    def __init__(self,
                 in_dim,
                 num_hidden,
                 out_dim,
                 num_layers,
                 nhead,
                 nhead_out,
                 activation,
                 feat_drop,
                 attn_drop,
                 negative_slope,
                 residual,
                 norm,
                 concat_out=False,
                 encoding=False,
                 aggregate_mode='sum'
                 ):
        super(GAT, self).__init__()
        self.out_dim = out_dim
        self.num_heads = nhead
        self.num_layers = num_layers
        self.gat_layers = nn.ModuleList()
        self.activation = activation
        self.concat_out = concat_out

        last_activation = create_activation(activation) if encoding else None
        last_residual = (encoding and residual)
        last_norm = norm if encoding else None

        if num_layers == 1:
            self.gat_layers.append(GATConv(
                in_dim, out_dim, nhead_out,
                feat_drop, attn_drop, negative_slope, last_residual, norm=last_norm, concat_out=concat_out))
        else:
            # input projection (no residual)
            self.gat_layers.append(GATConv(
                in_dim, num_hidden, nhead,
                feat_drop, attn_drop, negative_slope, residual, create_activation(activation), norm=norm,
                concat_out=concat_out))
            # hidden layers
            for l in range(1, num_layers - 1):
                # due to multi-head, the in_dim = num_hidden * num_heads
                self.gat_layers.append(GATConv(
                    num_hidden * nhead, num_hidden, nhead,
                    feat_drop, attn_drop, negative_slope, residual, create_activation(activation), norm=norm,
                    concat_out=concat_out))
            # output projection
            self.gat_layers.append(GATConv(
                num_hidden * nhead, out_dim, nhead_out,
                feat_drop, attn_drop, negative_slope, last_residual, activation=last_activation, norm=last_norm,
                concat_out=concat_out))

        # if norm is not None:
        #     self.norms = nn.ModuleList([
        #         norm(num_hidden * nhead)
        #         for _ in range(num_layers - 1)
        #     ])
        #     if self.concat_out:
        #         self.norms.append(norm(num_hidden * nhead))
        # else:
        #     self.norms = None

        self.head = nn.Identity()

    # def forward(self, g, inputs):
    #     h = inputs
    #     for l in range(self.num_layers):
    #         h = self.gat_layers[l](g, h)
    #         if l != self.num_layers - 1:
    #             h = h.flatten(1)
    #             if self.norms is not None:
    #                 h = self.norms[l](h)
    #     # output projection
    #     if self.concat_out:
    #         out = h.flatten(1)
    #         if self.norms is not None:
    #             out = self.norms[-1](out)
    #     else:
    #         out = h.mean(1)
    #     return self.head(out)

    def forward(self, g, inputs, return_hidden=False):
        h = inputs
        hidden_list = []
        for l in range(self.num_layers):
            h = self.gat_layers[l](g, h)
            hidden_list.append(h)
            # h = h.flatten(1)
        # output projection
        if return_hidden:
            return self.head(h), hidden_list
        else:
            return self.head(h)

    def reset_classifier(self, num_classes):
        self.head = nn.Linear(self.num_heads * self.out_dim, num_classes)


class GATConv(nn.Module):
    def __init__(self,
                 in_feats,
                 out_feats,
                 num_heads,
                 feat_drop=0.,
                 attn_drop=0.,
                 negative_slope=0.2,
                 residual=False,
                 activation=None,
                 allow_zero_in_degree=True,
                 bias=True,
                 norm=None,
                 concat_out=True):
        super(GATConv, self).__init__()
        self._num_heads = num_heads
        self._in_src_feats, self._in_dst_feats = expand_as_pair(in_feats)
        self._out_feats = out_feats
        self._allow_zero_in_degree = allow_zero_in_degree
        self._concat_out = concat_out

        if isinstance(in_feats, tuple):
            self.fc_src = nn.Linear(
                self._in_src_feats, out_feats * num_heads, bias=False)
            self.fc_dst = nn.Linear(
                self._in_dst_feats, out_feats * num_heads, bias=False)
        else:
            self.fc = nn.Linear(
                self._in_src_feats, out_feats * num_heads, bias=False)
        self.attn_l = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.attn_r = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.feat_drop = nn.Dropout(feat_drop)
        self.attn_drop = nn.Dropout(attn_drop)
        self.leaky_relu = nn.LeakyReLU(negative_slope)
        if bias:
            self.bias = nn.Parameter(torch.FloatTensor(size=(num_heads * out_feats,)))
        else:
            self.register_buffer('bias', None)
        if residual:
            if self._in_dst_feats != out_feats * num_heads:
                self.res_fc = nn.Linear(
                    self._in_dst_feats, num_heads * out_feats, bias=False)
            else:
                self.res_fc = nn.Identity()
        else:
            self.register_buffer('res_fc', None)
        self.reset_parameters()
        self.activation = activation
        # if norm is not None:
        #     self.norm = norm(num_heads * out_feats)
        # else:
        #     self.norm = None

        self.norm = norm
        if norm is not None:
            self.norm = norm(num_heads * out_feats)

    def reset_parameters(self):
        """

        Description
        -----------
        Reinitialize learnable parameters.

        Note
        ----
        The fc weights :math:`W^{(l)}` are initialized using Glorot uniform initialization.
        The attention weights are using xavier initialization method.
        """
        gain = nn.init.calculate_gain('relu')
        if hasattr(self, 'fc'):
            nn.init.xavier_normal_(self.fc.weight, gain=gain)
        else:
            nn.init.xavier_normal_(self.fc_src.weight, gain=gain)
            nn.init.xavier_normal_(self.fc_dst.weight, gain=gain)
        nn.init.xavier_normal_(self.attn_l, gain=gain)
        nn.init.xavier_normal_(self.attn_r, gain=gain)
        if self.bias is not None:
            nn.init.constant_(self.bias, 0)
        if isinstance(self.res_fc, nn.Linear):
            nn.init.xavier_normal_(self.res_fc.weight, gain=gain)

    def set_allow_zero_in_degree(self, set_value):
        self._allow_zero_in_degree = set_value

    def forward(self, graph, feat, get_attention=False):
        with graph.local_scope():
            if not self._allow_zero_in_degree:
                if (graph.in_degrees() == 0).any():
                    raise RuntimeError('There are 0-in-degree nodes in the graph, '
                                       'output for those nodes will be invalid. '
                                       'This is harmful for some applications, '
                                       'causing silent performance regression. '
                                       'Adding self-loop on the input graph by '
                                       'calling `g = dgl.add_self_loop(g)` will resolve '
                                       'the issue. Setting ``allow_zero_in_degree`` '
                                       'to be `True` when constructing this module will '
                                       'suppress the check and let the code run.')

            if isinstance(feat, tuple):
                src_prefix_shape = feat[0].shape[:-1]
                dst_prefix_shape = feat[1].shape[:-1]
                h_src = self.feat_drop(feat[0])
                h_dst = self.feat_drop(feat[1])
                if not hasattr(self, 'fc_src'):
                    feat_src = self.fc(h_src).view(
                        *src_prefix_shape, self._num_heads, self._out_feats)
                    feat_dst = self.fc(h_dst).view(
                        *dst_prefix_shape, self._num_heads, self._out_feats)
                else:
                    feat_src = self.fc_src(h_src).view(
                        *src_prefix_shape, self._num_heads, self._out_feats)
                    feat_dst = self.fc_dst(h_dst).view(
                        *dst_prefix_shape, self._num_heads, self._out_feats)
            else:
                src_prefix_shape = dst_prefix_shape = feat.shape[:-1]
                h_src = h_dst = self.feat_drop(feat)
                feat_src = feat_dst = self.fc(h_src).view(
                    *src_prefix_shape, self._num_heads, self._out_feats)
                if graph.is_block:
                    feat_dst = feat_src[:graph.number_of_dst_nodes()]
                    h_dst = h_dst[:graph.number_of_dst_nodes()]
                    dst_prefix_shape = (graph.number_of_dst_nodes(),) + dst_prefix_shape[1:]
            # NOTE: GAT paper uses "first concatenation then linear projection"
            # to compute attention scores, while ours is "first projection then
            # addition", the two approaches are mathematically equivalent:
            # We decompose the weight vector a mentioned in the paper into
            # [a_l || a_r], then
            # a^T [Wh_i || Wh_j] = a_l Wh_i + a_r Wh_j
            # Our implementation is much efficient because we do not need to
            # save [Wh_i || Wh_j] on edges, which is not memory-efficient. Plus,
            # addition could be optimized with DGL's built-in function u_add_v,
            # which further speeds up computation and saves memory footprint.
            el = (feat_src * self.attn_l).sum(dim=-1).unsqueeze(-1)
            er = (feat_dst * self.attn_r).sum(dim=-1).unsqueeze(-1)
            graph.srcdata.update({'ft': feat_src, 'el': el})
            graph.dstdata.update({'er': er})
            # compute edge attention, el and er are a_l Wh_i and a_r Wh_j respectively.
            graph.apply_edges(fn.u_add_v('el', 'er', 'e'))
            e = self.leaky_relu(graph.edata.pop('e'))
            # e[e == 0] = -1e3
            # e = graph.edata.pop('e')
            # compute softmax
            graph.edata['a'] = self.attn_drop(edge_softmax(graph, e))
            # message passing
            graph.update_all(fn.u_mul_e('ft', 'a', 'm'),
                             fn.sum('m', 'ft'))
            rst = graph.dstdata['ft']

            # bias
            if self.bias is not None:
                rst = rst + self.bias.view(
                    *((1,) * len(dst_prefix_shape)), self._num_heads, self._out_feats)

            # residual
            if self.res_fc is not None:
                # Use -1 rather than self._num_heads to handle broadcasting
                resval = self.res_fc(h_dst).view(*dst_prefix_shape, -1, self._out_feats)
                rst = rst + resval

            if self._concat_out:
                rst = rst.flatten(1)
            else:
                rst = torch.mean(rst, dim=1)

            if self.norm is not None:
                rst = self.norm(rst)

            # activation
            if self.activation:
                rst = self.activation(rst)

            if get_attention:
                return rst, graph.edata['a']
            else:
                return rst


class GCN(nn.Module):
    def __init__(self,
                 in_dim,
                 num_hidden,
                 out_dim,
                 num_layers,
                 dropout,
                 activation,
                 residual,
                 norm,
                 encoding=False
                 ):
        super(GCN, self).__init__()
        self.out_dim = out_dim
        self.num_layers = num_layers
        self.gcn_layers = nn.ModuleList()
        self.activation = activation
        self.dropout = dropout

        last_activation = create_activation(activation) if encoding else None
        last_residual = encoding and residual
        last_norm = norm if encoding else None

        if num_layers == 1:
            self.gcn_layers.append(GraphConv(
                in_dim, out_dim, residual=last_residual, norm=last_norm, activation=last_activation))
        else:
            # input projection (no residual)
            self.gcn_layers.append(GraphConv(
                in_dim, num_hidden, residual=residual, norm=norm, activation=create_activation(activation)))
            # hidden layers
            for l in range(1, num_layers - 1):
                # due to multi-head, the in_dim = num_hidden * num_heads
                self.gcn_layers.append(GraphConv(
                    num_hidden, num_hidden, residual=residual, norm=norm, activation=create_activation(activation)))
            # output projection
            self.gcn_layers.append(GraphConv(
                num_hidden, out_dim, residual=last_residual, activation=last_activation, norm=last_norm))

        # if norm is not None:
        #     self.norms = nn.ModuleList([
        #         norm(num_hidden)
        #         for _ in range(num_layers - 1)
        #     ])
        #     if not encoding:
        #         self.norms.append(norm(out_dim))
        # else:
        #     self.norms = None
        self.norms = None
        self.head = nn.Identity()

    def forward(self, g, inputs, return_hidden=False):
        h = inputs
        hidden_list = []
        for l in range(self.num_layers):
            h = F.dropout(h, p=self.dropout, training=self.training)
            h = self.gcn_layers[l](g, h)
            if self.norms is not None and l != self.num_layers - 1:
                h = self.norms[l](h)
            hidden_list.append(h)
        # output projection
        if self.norms is not None and len(self.norms) == self.num_layers:
            h = self.norms[-1](h)
        if return_hidden:
            return self.head(h), hidden_list
        else:
            return self.head(h)

    def reset_classifier(self, num_classes):
        self.head = nn.Linear(self.out_dim, num_classes)


class GraphConv(nn.Module):
    def __init__(self,
                 in_dim,
                 out_dim,
                 norm=None,
                 activation=None,
                 residual=True,
                 ):
        super().__init__()
        self._in_feats = in_dim
        self._out_feats = out_dim

        self.fc = nn.Linear(in_dim, out_dim)

        if residual:
            if self._in_feats != self._out_feats:
                self.res_fc = nn.Linear(
                    self._in_feats, self._out_feats, bias=False)
                print("! Linear Residual !")
            else:
                print("Identity Residual ")
                self.res_fc = nn.Identity()
        else:
            self.register_buffer('res_fc', None)

        # if norm == "batchnorm":
        #     self.norm = nn.BatchNorm1d(out_dim)
        # elif norm == "layernorm":
        #     self.norm = nn.LayerNorm(out_dim)
        # else:
        #     self.norm = None

        self.norm = norm
        if norm is not None:
            self.norm = norm(out_dim)
        self._activation = activation

        self.reset_parameters()

    def reset_parameters(self):
        self.fc.reset_parameters()

    def forward(self, graph, feat):
        with graph.local_scope():
            # aggregate_fn = fn.copy_src('h', 'm')
            def custom_copy_src(edges):
                return {'m': edges.src['h']}

            aggregate_fn = custom_copy_src
            # if edge_weight is not None:
            #     assert edge_weight.shape[0] == graph.number_of_edges()
            #     graph.edata['_edge_weight'] = edge_weight
            #     aggregate_fn = fn.u_mul_e('h', '_edge_weight', 'm')

            # (BarclayII) For RGCN on heterogeneous graphs we need to support GCN on bipartite.
            feat_src, feat_dst = expand_as_pair(feat, graph)
            # if self._norm in ['left', 'both']:
            degs = graph.out_degrees().float().clamp(min=1)
            norm = torch.pow(degs, -0.5)
            shp = norm.shape + (1,) * (feat_src.dim() - 1)
            norm = torch.reshape(norm, shp)
            feat_src = feat_src * norm

            # if self._in_feats > self._out_feats:
            #     # mult W first to reduce the feature size for aggregation.
            #     # if weight is not None:
            #         # feat_src = th.matmul(feat_src, weight)
            #     graph.srcdata['h'] = feat_src
            #     graph.update_all(aggregate_fn, fn.sum(msg='m', out='h'))
            #     rst = graph.dstdata['h']
            # else:
            # aggregate first then mult W
            graph.srcdata['h'] = feat_src
            graph.update_all(aggregate_fn, fn.sum(msg='m', out='h'))
            rst = graph.dstdata['h']

            rst = self.fc(rst)

            # if self._norm in ['right', 'both']:
            degs = graph.in_degrees().float().clamp(min=1)
            norm = torch.pow(degs, -0.5)
            shp = norm.shape + (1,) * (feat_dst.dim() - 1)
            norm = torch.reshape(norm, shp)
            rst = rst * norm

            if self.res_fc is not None:
                rst = rst + self.res_fc(feat_dst)

            if self.norm is not None:
                rst = self.norm(rst)

            if self._activation is not None:
                rst = self._activation(rst)

            return rst


class GIN(nn.Module):
    def __init__(self,
                 in_dim,
                 num_hidden,
                 out_dim,
                 num_layers,
                 dropout,
                 activation,
                 residual,
                 norm,
                 encoding=False,
                 learn_eps=False,
                 aggr="sum",
                 ):
        super(GIN, self).__init__()
        self.out_dim = out_dim
        self.num_layers = num_layers
        self.layers = nn.ModuleList()
        self.activation = activation
        self.dropout = dropout

        last_activation = create_activation(activation) if encoding else None
        last_residual = encoding and residual
        last_norm = norm if encoding else None

        if num_layers == 1:
            apply_func = MLP(2, in_dim, num_hidden, out_dim, activation=activation, norm=norm)
            if last_norm:
                apply_func = ApplyNodeFunc(apply_func, norm=norm, activation=activation)
            self.layers.append(
                GINConv(in_dim, out_dim, apply_func, init_eps=0, learn_eps=learn_eps, residual=last_residual))
        else:
            # input projection (no residual)
            self.layers.append(GINConv(
                in_dim,
                num_hidden,
                ApplyNodeFunc(MLP(2, in_dim, num_hidden, num_hidden, activation=activation, norm=norm),
                              activation=activation, norm=norm),
                init_eps=0,
                learn_eps=learn_eps,
                residual=residual)
            )
            # hidden layers
            for l in range(1, num_layers - 1):
                # due to multi-head, the in_dim = num_hidden * num_heads
                self.layers.append(GINConv(
                    num_hidden, num_hidden,
                    ApplyNodeFunc(MLP(2, num_hidden, num_hidden, num_hidden, activation=activation, norm=norm),
                                  activation=activation, norm=norm),
                    init_eps=0,
                    learn_eps=learn_eps,
                    residual=residual)
                )
            # output projection
            apply_func = MLP(2, num_hidden, num_hidden, out_dim, activation=activation, norm=norm)
            if last_norm:
                apply_func = ApplyNodeFunc(apply_func, activation=activation, norm=norm)

            self.layers.append(
                GINConv(num_hidden, out_dim, apply_func, init_eps=0, learn_eps=learn_eps, residual=last_residual))

        self.head = nn.Identity()

    def forward(self, g, inputs, return_hidden=False):
        h = inputs
        hidden_list = []
        for l in range(self.num_layers):
            h = F.dropout(h, p=self.dropout, training=self.training)
            h = self.layers[l](g, h)
            hidden_list.append(h)
        # output projection
        if return_hidden:
            return self.head(h), hidden_list
        else:
            return self.head(h)

    def reset_classifier(self, num_classes):
        self.head = nn.Linear(self.out_dim, num_classes)


class GINConv(nn.Module):
    def __init__(self,
                 in_dim,
                 out_dim,
                 apply_func,
                 aggregator_type="sum",
                 init_eps=0,
                 learn_eps=False,
                 residual=False,
                 ):
        super().__init__()
        self._in_feats = in_dim
        self._out_feats = out_dim
        self.apply_func = apply_func

        self._aggregator_type = aggregator_type
        if aggregator_type == 'sum':
            self._reducer = fn.sum
        elif aggregator_type == 'max':
            self._reducer = fn.max
        elif aggregator_type == 'mean':
            self._reducer = fn.mean
        else:
            raise KeyError('Aggregator type {} not recognized.'.format(aggregator_type))

        if learn_eps:
            self.eps = torch.nn.Parameter(torch.FloatTensor([init_eps]))
        else:
            self.register_buffer('eps', torch.FloatTensor([init_eps]))

        if residual:
            if self._in_feats != self._out_feats:
                self.res_fc = nn.Linear(
                    self._in_feats, self._out_feats, bias=False)
                print("! Linear Residual !")
            else:
                print("Identity Residual ")
                self.res_fc = nn.Identity()
        else:
            self.register_buffer('res_fc', None)

    def forward(self, graph, feat):
        with graph.local_scope():
            def custom_copy_src(edges):
                return {'m': edges.src['h']}

            # aggregate_fn = fn.copy_src('h', 'm')

            feat_src, feat_dst = expand_as_pair(feat, graph)
            graph.srcdata['h'] = feat_src
            graph.update_all(custom_copy_src, self._reducer('m', 'neigh'))
            rst = (1 + self.eps) * feat_dst + graph.dstdata['neigh']
            if self.apply_func is not None:
                rst = self.apply_func(rst)

            if self.res_fc is not None:
                rst = rst + self.res_fc(feat_dst)

            return rst


class ApplyNodeFunc(nn.Module):
    """Update the node feature hv with MLP, BN and ReLU."""

    def __init__(self, mlp, norm="batchnorm", activation="prelu"):
        super(ApplyNodeFunc, self).__init__()
        self.mlp = mlp
        norm_func = create_norm(norm)
        if norm_func is None:
            self.norm = nn.Identity()
        else:
            self.norm = norm_func(self.mlp.output_dim)
        self.act = create_activation(activation)

    def forward(self, h):
        h = self.mlp(h)
        h = self.norm(h)
        h = self.act(h)
        return h


class MLP(nn.Module):
    """MLP with linear output"""

    def __init__(self, num_layers, input_dim, hidden_dim, output_dim, activation="prelu", norm="layernorm"):
        super(MLP, self).__init__()
        self.linear_or_not = True  # default is linear model
        self.num_layers = num_layers
        self.output_dim = output_dim

        if num_layers < 1:
            raise ValueError("number of layers should be positive!")
        elif num_layers == 1:
            # Linear model
            self.linear = nn.Linear(input_dim, output_dim)
        else:
            # Multi-layer model
            self.linear_or_not = False
            self.linears = torch.nn.ModuleList()
            self.norms = torch.nn.ModuleList()
            self.activations = torch.nn.ModuleList()

            self.linears.append(nn.Linear(input_dim, hidden_dim))
            for layer in range(num_layers - 2):
                self.linears.append(nn.Linear(hidden_dim, hidden_dim))
            self.linears.append(nn.Linear(hidden_dim, output_dim))

            for layer in range(num_layers - 1):
                self.norms.append(create_norm(norm)(hidden_dim))
                self.activations.append(create_activation(activation))

    def forward(self, x):
        if self.linear_or_not:
            # If linear model
            return self.linear(x)
        else:
            # If MLP
            h = x
            for i in range(self.num_layers - 1):
                h = self.norms[i](self.linears[i](h))
                h = self.activations[i](h)
            return self.linears[-1](h)