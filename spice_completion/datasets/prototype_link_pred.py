"""
    This creates a dataset where X is the graph where one component has been removed.
    "Action nodes" have been added for the possible actions to take and link prediction
    should be performed between all nodes and all possible (node) connections.
"""
import numpy as np
import random
from . import helpers as h
from spektral.data import Dataset, Graph
import scipy.sparse as sp
import itertools
import torch
import deepsnap

all_component_types = h.component_types
embedding_size = len(all_component_types) + 1
action_index = len(all_component_types)

class PrototypeLinkDataset(Dataset):
    def __init__(self, filenames, resample=True, normalize=True, train=True, mean=None, std=None, **kwargs):
        self.filenames = h.valid_netlist_sources(filenames)
        self.resample = resample
        self.normalize = normalize
        self.train = train
        if normalize:
            self.mean = mean
            self.std = std
        else:
            self.mean = 0
            self.std = 1
        self.epsilon = 0.
        super().__init__(**kwargs)

    def read(self):
        graphs = []
        for filename in self.filenames:
            graphs.extend(self.load_graphs(filename))

        if self.resample:
            graphs_by_label = {}
            for (i, graph) in enumerate(graphs):
                label = self.graph_label_type(graph)
                if label not in graphs_by_label:
                    graphs_by_label[label] = []
                graphs_by_label[label].append(i)

            counts = [ len(vals) for vals in graphs_by_label.values() ]
            counts.sort()
            middle_idx = len(counts)//2
            median_count = counts[middle_idx]
            median_count = min(counts)
            print(f'Resampling classes to median size ({median_count})')

            graph_idx = []
            for label_idx in graphs_by_label.values():
                if len(label_idx) > median_count:
                    idx = random.sample(label_idx, median_count)
                else:
                    idx = label_idx

                graph_idx.extend(idx)

            graphs = [ graphs[i] for i in graph_idx ]

        if self.normalize:
            graphs = self.normalize_graphs(graphs)

        return graphs

    def unnormalize(self, graph_nodes):
        return (graph_nodes * (self.std + self.epsilon)) + self.mean

    def normalize_graphs(self, graphs):
        if self.mean is None or self.std is None:
            node_count = sum(( graph.x.shape[0] for graph in graphs ))
            graph_nodes = np.concatenate([ graph.x for graph in graphs ], axis=0)
            mean = np.sum(graph_nodes, axis=0) / node_count
            residuals = graph_nodes - mean
            raw_std = np.sum(residuals, axis=0) / node_count
            nonzero_idx = raw_std.nonzero()[0]
            std = np.ones(raw_std.shape)
            std[nonzero_idx] = raw_std[nonzero_idx]
            self.mean = mean
            self.std = std

        for graph in graphs:
            graph.x = (graph.x - self.mean) / (self.std + self.epsilon)

        return graphs


    def graph_label_type(self, graph):
        label_idx = np.argmax(graph.y)
        class_idx = np.argmax(graph.x[label_idx])
        return class_idx

    def get_node_types(self, nodes, normalized=True):
        if normalized:
            nodes = self.unnormalize(nodes)
        node_types = np.argmax(nodes > 0.99999, axis=1)
        return node_types

    @staticmethod
    def load_graph(components, adj, omitted_idx=None):
        component_count = len(components)
        action_component_count = len(all_component_types)
        if omitted_idx is not None:
            total_components = component_count + action_component_count - 1
        else:
            total_components = component_count + action_component_count

        component_types = np.array([ h.get_component_type_index(c) for c in components ])

        # nodes...
        x = np.zeros((total_components, embedding_size))
        x[np.arange(component_types.size), component_types] = 1

        # prototype nodes...
        action_offset = component_types.size
        num_actions = len(all_component_types)
        action_indices = np.zeros(num_actions).astype(int)
        if omitted_idx is not None:
            action_indices[0] = omitted_idx
            action_indices[1:] = np.arange(action_offset, action_offset + num_actions - 1)

            omitted_type = component_types[omitted_idx]
            action_types = [idx for idx in range(len(all_component_types)) if idx != omitted_type]
            action_types.insert(0, omitted_type)
        else:
            action_indices = np.arange(action_offset, action_offset + num_actions)
            action_types = [idx for idx in range(len(all_component_types))]

        action_types = np.array(action_types).astype(int)
        x[action_indices, action_index] = 1
        x[action_indices, action_types] = 1

        expanded_adj = np.zeros((x.shape[0], x.shape[0]))
        expanded_adj[0:adj.shape[0], 0:adj.shape[1]] = adj

        # if self.shuffle:
            # indices = np.arange(x.shape[0])
            # np.random.shuffle(indices)
            # x = np.take(x, indices, axis=0)
            # y = np.take(y, indices, axis=0)
            # expanded_adj = np.take(expanded_adj, indices, axis=0)

        a = sp.csr_matrix(expanded_adj)
        return Graph(x=x, a=a)

    def load_graphs(self, source):
        (components, adj) = h.netlist_as_graph(source)
        if self.train:
            count = len(components)
            graphs = ( self.load_graph(components, adj, omitted_idx) for omitted_idx in range(count) )
        else:
            graphs = [ self.load_graph(components, adj) ]
        return graphs

    def to_deepsnap(self):
        graphs = []
        nxgraphs = h.to_networkx(self)
        src_graphs = zip((sgraph for sgraph in self), nxgraphs)

        for (sgraph, nxgraph) in src_graphs:
            node_features = torch.tensor(sgraph.x)
            h.ensure_no_nan(node_features)

            # FIXME: set the edge_labels
            edge_label_index = torch.tensor([[0, 0, 0, 0], [1, 2, 3, 4]])
            edge_label = torch.ones((4,))
            graph = deepsnap.graph.Graph(nxgraph, edge_label_index=edge_label_index, edge_label=edge_label)

            graphs.append(graph)

        return graphs


def load(filenames, **kwargs):
    return PrototypeLinkDataset(filenames, **kwargs)
