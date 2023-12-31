# Build and train a graph convolutional neural network using PyTorch Geometric for the node property prediction task.
# 
# We will use ogbn-products dataset.

# ## OGBN-Products

# The ogbn-products dataset is an undirected and unweighted graph, representing an Amazon product co-purchasing network. Nodes represent products sold in Amazon, and edges between two products indicate that the products are purchased together. Node features are generated by extracting bag-of-words features from the product descriptions followed by a Principal Component Analysis to reduce the dimension to 100.
# 
# The task is to predict the category of a product in a multi-class classification setup, where the 47 top-level categories are used for target labels.
import torch
import os
print("PyTorch has version {}".format(torch.__version__))


# Download the necessary packages for PyG. Make sure that your version of torch matches the output from the cell above. In case of any issues, more information can be found on the [PyG's installation page](https://pytorch-geometric.readthedocs.io/en/latest/notes/installation.html).

# Install torch geometric
get_ipython().system('pip install torch-scatter -f https://pytorch-geometric.com/whl/torch-{torch.__version__}.html')
get_ipython().system('pip install torch-sparse -f https://pytorch-geometric.com/whl/torch-{torch.__version__}.html')
get_ipython().system('pip install torch-geometric')
get_ipython().system('pip install ogb')

from ogb.nodeproppred import PygNodePropPredDataset, Evaluator
import torch
import torch.nn.functional as F
from torch_geometric.nn import GCNConv
import torch_geometric.transforms as T
from torch_geometric.data import DataLoader
import numpy as np
from torch_geometric.typing import SparseTensor
import torch.nn as nn


# ## Load and Preprocess the Dataset
dataset_name = 'ogbn-products'
dataset = PygNodePropPredDataset(name=dataset_name,
                                 transform=T.ToSparseTensor())
data = dataset[0]

device = 'cuda' if torch.cuda.is_available() else 'cpu'

# If you use GPU, the device should be cuda
print('Device: {}'.format(device))


# This dataset is very big and if you try to run it as it is on colab, you may get an out of memory error.
# 
# One solution is to use batching and train on subgraphs. Here, we will just make a smaller dataset so that we can train it in one go.

# We need to have edge indxes to make a subgraph. We can get those from the adjacency matrix.
data.edge_index = torch.stack([data.adj_t.__dict__["storage"]._row, data.adj_t.__dict__["storage"]._col])

# We will only use the first 100000 nodes.
sub_nodes = 100000
sub_graph = data.subgraph(torch.arange(sub_nodes))

# Update the adjaceny matrix according to the new graph
sub_graph.adj_t = SparseTensor(
    row=sub_graph.edge_index[0],
    col=sub_graph.edge_index[1],
    sparse_sizes=None,
    is_sorted=True,
    trust_data=True,
)

sub_graph = sub_graph.to(device)

data = sub_graph
data = data.to(device)

# Spilt data into train validation and test set
split_sizes = [int(sub_nodes*0.8),int(sub_nodes*0.05),int(sub_nodes*0.15)]
indices = torch.arange(sub_nodes)
np.random.shuffle(indices.numpy())
split_idx = {s:t for t,s in zip(torch.split(indices, split_sizes, dim=0), ["train", "valid", "test"])}
split_idx

train_idx = split_idx['train'].to(device)


print(f"Feature Length of each node: {data.x.shape[1]}")


class GCN(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim, return_embeds=False):
        super(GCN, self).__init__()
        self.gcnlayers = nn.ModuleList([
            GCNConv(input_dim, hidden_dim),
            GCNConv(hidden_dim, hidden_dim),
            GCNConv(hidden_dim, hidden_dim),
            GCNConv(hidden_dim, hidden_dim)
        ])
        self.last_gcn = GCNConv(hidden_dim, output_dim)
        self.bns = nn.ModuleList([
            nn.BatchNorm1d(hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.BatchNorm1d(hidden_dim)
        ])
        self.softmax = nn.LogSoftmax()
        self.return_embeds = return_embeds

    def reset_parameters(self):
        for conv in self.gcnlayers:
            conv.reset_parameters()
        for bn in self.bns:
            bn.reset_parameters()

    def forward(self, x, adj_t):

        for i in range(len(self.gcnlayers)):
            x = self.gcnlayers[i](x, adj_t)
            x = self.bns[i](x)
            x = F.relu(x)
            x = F.dropout(x, p=0.5, training=self.training)
        x = self.last_gcn(x, adj_t)

        if not self.return_embeds:
          x = self.softmax(x)
        return x


# In[28]:


def train(model, data, train_idx, optimizer, loss_fn):
    model.train()
    loss = 0

    optimizer.zero_grad()
    out = model(data.x, data.adj_t)
    loss = loss_fn(out[train_idx], data.y[train_idx].reshape(-1))

    loss.backward()
    optimizer.step()

    return loss.item()


# In[29]:



@torch.no_grad()
def test(model, data, split_idx, evaluator, save_model_results=False):
    model.eval()

    out = model(data.x, data.adj_t)

    y_pred = out.argmax(dim=-1, keepdim=True)

    train_acc = evaluator.eval({
        'y_true': data.y[split_idx['train']],
        'y_pred': y_pred[split_idx['train']],
    })['acc']
    valid_acc = evaluator.eval({
        'y_true': data.y[split_idx['valid']],
        'y_pred': y_pred[split_idx['valid']],
    })['acc']
    test_acc = evaluator.eval({
        'y_true': data.y[split_idx['test']],
        'y_pred': y_pred[split_idx['test']],
    })['acc']

    if save_model_results:
      print ("Saving Model Predictions")

      data = {}
      data['y_pred'] = y_pred.view(-1).cpu().detach().numpy()

      df = pd.DataFrame(data=data)
      df.to_csv('ogbn-products_node.csv', sep=',', index=False)


    return train_acc, valid_acc, test_acc

args = {
    'device': device,
    'hidden_dim': 256,
    'lr': 0.01,
    'epochs': 200,
}

model = GCN(data.num_features, args['hidden_dim'],
            dataset.num_classes).to(device)
evaluator = Evaluator(name='ogbn-products')


import copy

model.reset_parameters()

optimizer = torch.optim.Adam(model.parameters(), lr=args['lr'])


best_model = None
best_valid_acc = 0

for epoch in range(1, 1 + args["epochs"]):
  model.train()
  optimizer.zero_grad()
  out = model(data.x, data.adj_t)
  loss = F.nll_loss(out[train_idx], data.y[train_idx].reshape(-1))
  loss.backward()
  optimizer.step()
  result = test(model, data, split_idx, evaluator)
  train_acc, valid_acc, test_acc = result
  if valid_acc > best_valid_acc:
      best_valid_acc = valid_acc
      best_model = copy.deepcopy(model)
  print(f'Epoch: {epoch:02d}, '
        f'Loss: {loss.item():.4f}, '
        f'Train: {100 * train_acc:.2f}%, '
        f'Valid: {100 * valid_acc:.2f}% '
        f'Test: {100 * test_acc:.2f}%')

