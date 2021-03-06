import numpy as np
import math
import os

"""
This example reuses the Graph Attn Network from the paper:

Graph Attention Networks (https://arxiv.org/abs/1710.10903)
Petar Veličković, Guillem Cucurull, Arantxa Casanova, Adriana Romero, Pietro Liò, Yoshua Bengio
"""

from datetime import datetime
import tensorflow as tf
from tensorflow.keras.callbacks import EarlyStopping
from tensorflow.keras.losses import CategoricalCrossentropy
from tensorflow.keras.metrics import CategoricalAccuracy
from tensorflow.keras.layers import Input, Dropout
from tensorflow.keras.models import Model
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.regularizers import l2

from spektral.data import DisjointLoader
from spektral.layers import GATConv

import types
import datasets
import sys
import argparse
from model import model
import json

parser = argparse.ArgumentParser()
parser.add_argument('files', nargs='+')
parser.add_argument('--name', default='train_actions')
parser.add_argument('--epochs', default=100, type=int)
parser.add_argument('--batch_size', default=32, type=int)
parser.add_argument('--lr', default=0.005, type=float)
parser.add_argument('--checkpoint-freq', default=-1, type=int)
parser.add_argument('--seed', default=1234, type=float)
args = parser.parse_args()

# Configure tensorboard stuff
logdir = f'logs/{args.name}/' + datetime.now().strftime('%Y%m%d-%H%M%S')
file_writer = tf.summary.create_file_writer(logdir + "/metrics")
file_writer.set_as_default()
exp_config = vars(args)
with open(f'{logdir}/configuration.json', 'w') as f:
    json.dump(exp_config, f)
exp_config = types.SimpleNamespace(**exp_config)

np.random.seed(exp_config.seed)
tf.random.set_seed(exp_config.seed)
batch_size = exp_config.batch_size
epochs = exp_config.epochs

# Load data
dataset = datasets.omitted_with_actions(exp_config.files, shuffle=False)
#dataset = dataset[0:2]
#np.set_printoptions(threshold=100000)

# Train/valid/test split
idxs = np.random.permutation(len(dataset))
split_va, split_te = int(0.8 * len(dataset)), int(0.9 * len(dataset))
idx_tr, idx_va, idx_te = np.split(idxs, [split_va, split_te])
dataset_tr = dataset[idx_tr]
dataset_va = dataset[idx_va]
dataset_te = dataset[idx_te]

print('dataset size:', len(dataset))
dataset_tr = dataset  # FIXME: Using "entire" dataset for now
loader_tr = DisjointLoader(dataset_tr, batch_size=batch_size, epochs=epochs)
loader_va = DisjointLoader(dataset_va, batch_size=batch_size)
loader_te = DisjointLoader(dataset_te, batch_size=batch_size)

# Parameters
channels = 8            # Number of channel in each head of the first GAT layer
n_attn_heads = 8        # Number of attention heads in first GAT layer
F = dataset.n_node_features
dropout = 0.6           # Dropout rate for the features and adjacency matrix
dropout = 0.  # FIXME: remove
l2_reg = 5e-6           # L2 regularization rate
learning_rate = exp_config.lr
epochs = exp_config.epochs
es_patience = 100       # Patience for early stopping

# Model definition
loss_fn = CategoricalCrossentropy()
opt = Adam(lr=learning_rate)
#model.compile(optimizer=optimizer,
              #weighted_metrics=['acc'])
acc_fn = CategoricalAccuracy()
model.summary()

def softmax_ragged(x):
    max_x = tf.math.reduce_max(x)
    logits = tf.math.exp(x - max_x)
    sums = tf.expand_dims(tf.reduce_sum(logits, 1), axis=1)
    action_probs = tf.divide(logits.to_tensor(), sums)

    # Naive (unstable) implementation
    #logits = tf.math.exp(x)
    #sums = tf.expand_dims(tf.reduce_sum(logits, 1), axis=1)
    #action_probs = tf.divide(logits.to_tensor(), sums)
    return action_probs

def forward(model, inputs, target, training=True):
    nodes, adj, edges = inputs
    output = model((nodes, adj), training=training)
    lens = [ len(graph_y) for graph_y in target ]

    output = tf.squeeze(output, axis=1)
    output = tf.RaggedTensor.from_row_lengths(output, lens)
    flat_targets = np.hstack(target)
    target_rt = tf.RaggedTensor.from_row_lengths(flat_targets, lens)
    mask = tf.math.not_equal(target_rt, -1)
    logits = tf.ragged.boolean_mask(output, mask)

    action_probs = softmax_ragged(logits)

    target = tf.ragged.boolean_mask(target_rt, mask)
    target = tf.reshape(target.to_tensor(), action_probs.shape)

    return action_probs, target, mask

print('Fitting model')
current_batch = epoch = model_loss = model_acc = iteration = 0
best_val_loss = np.inf
best_weights = None
patience = es_patience
losses = []
accuracies = []
learning_layers_idx = [ i for (i, layer) in enumerate(model.layers) if len(layer.weights) > 0 ]

def log_gradients(gradients):
    nonzero_grads = []
    for i in learning_layers_idx:
        nonzero_grads.append(gradients[i])
        tf.summary.scalar(f'{model.layers[i].name} gradient norm', data=np.linalg.norm(gradients[i]), step=iteration)
        tf.summary.histogram(f'{model.layers[i].name} weights ({model.layers[i].weights[0].shape})', data=model.layers[i].weights[0], step=iteration)
        tf.summary.histogram(f'{model.layers[i].name} gradients', data=gradients[i], step=iteration)

    grad_norm = sum((np.linalg.norm(g) for g in nonzero_grads)) / len(nonzero_grads)
    tf.summary.scalar('mean gradient norm', data=grad_norm, step=iteration)

def distribution_as_histogram(distribution, precision=0.01):
    dist_as_histogram = []
    for (i, prob) in enumerate(distribution):
        count = prob.numpy()/precision + 1
        for _ in range(int(count)):
            dist_as_histogram.append(i)
    return np.array(dist_as_histogram)

def log_sample_prediction(point, epoch, prediction, target):
    print('>>> sample_prediction:', np.argmax(prediction), np.argmax(target), f'({target.shape})')
    try:
        prediction_dist = distribution_as_histogram(prediction)
        tf.summary.histogram(f'{point}. Sample Prediction ({np.argmax(target)})', prediction_dist, step=epoch, buckets=len(prediction))
    except Exception as e:
        print('Unable to convert prediction to histogram!')
        print(prediction)
        raise e

    target_dist = distribution_as_histogram(target)
    tf.summary.histogram('Sample Target', target_dist, step=epoch, buckets=len(target))

DEBUG = {}
# Train model
#@tf.function(input_signature=loader_tr.tf_signature(), experimental_relax_shapes=True)
def train_step(inputs, targets):
    with tf.GradientTape() as tape:
        action_probs, target, mask = forward(model, inputs, targets)

        loss = loss_fn(target, action_probs)
        loss += sum(model.losses)

        print('---- Computing accuracy ----')
        log_prediction(inputs[0], target, action_probs, mask)
        # print('target', target, np.argmax(target, axis=1))
        # print('action_probs', action_probs, np.argmax(action_probs, axis=1))
        # print('inputs[0].shape', inputs[0].shape)
        # print('targets.shape', targets.shape)
        # graph_size = targets.shape[1]
        # idx_row_col = (targets > -1).nonzero()
        # idx = [  i*graph_size + idx for (i, idx) in zip(idx_row_col[0], idx_row_col[1]) ]
        # prototypes = inputs[0][idx]
        # node_types = dataset.get_node_types(prototypes)
        # # print('(prototype) node_types:', node_types)
        # proto_size = prototypes.shape[1]
        # target_idx = [ idx + i*proto_size for (i, idx) in enumerate(np.argmax(target, axis=1))]
        # pred_idx = [ idx + i*proto_size for (i, idx) in enumerate(np.argmax(action_probs, axis=1))]
        # pred_types = [ node_types[idx] for idx in pred_idx ]
        # target_types = [ node_types[idx] for idx in target_idx ]
        #print('Predictions:', pred_types, f'({target_types})')

        # TODO: get the types?
        acc = acc_fn(target, action_probs)
        acc_fn.reset_states()
    gradients = tape.gradient(loss, model.trainable_variables)
    log_gradients(gradients)
    for grad in gradients:
        has_nan = tf.math.count_nonzero(tf.math.is_nan(grad))
        if has_nan:
            global DEBUG
            DEBUG['gradients'] = gradients
            DEBUG['action_probs'] = action_probs
            DEBUG['target'] = target
            DEBUG['inputs'] = inputs
            DEBUG['loss'] = loss
            DEBUG['acc'] = acc
            print('gradient has a nan!')
            exit()
    # TODO: clip gradients?
    opt.apply_gradients(zip(gradients, model.trainable_variables))

    return action_probs, target, loss, acc

def evaluate(loader, ops_list):
    output = []
    step = 0
    while step < loader.steps_per_epoch:
        step += 1
        (nodes, adj, edges), target = loader.__next__()
        pred = model((nodes, adj), training=False)
        outs = [o(target, pred) for o in ops_list]
        output.append(outs)
    return np.mean(output, 0)

def select_prototype_types(prototype_types, actions):
    node_count = actions.shape[1]
    pred_idx = np.array([idx + i*node_count for (i, idx) in enumerate(np.argmax(actions, axis=1))])
    #print('--- idx', pred_idx, np.argmax(actions, axis=1), node_count, prototype_types.shape)
    pred_types = np.take(prototype_types, pred_idx)
    return pred_types

def log_prediction(nodes, targets, predictions, mask):
    node_types = dataset.get_node_types(nodes)
    flat_mask = np.hstack(mask)
    prototype_types = tf.boolean_mask(node_types, flat_mask)
    #print('prototype_types', prototype_types)
    print('Predictions', np.argmax(predictions, axis=1), f'({np.argmax(targets, axis=1)})')

    pred_types = select_prototype_types(prototype_types, predictions)
    actual_types = select_prototype_types(prototype_types, targets)
    print('  Types:', pred_types, f'({actual_types})')
    return pred_types, actual_types

def save_checkpoint(name, model):
    os.makedirs(f'{logdir}/{name}', exist_ok=True)
    loader = DisjointLoader(dataset_tr, batch_size=batch_size, epochs=1)
    all_pred_types = []
    all_actual_types = []
    print('>>> saving checkpoint <<<')
    for batch in loader:
        nodes, adj, edges = batch[0]
        actions, targets, mask = forward(model, *batch, training=False)
        pred_types, actual_types = log_prediction(nodes, targets, actions, mask)
        print('pred_types:', pred_types)
        print('actual_types:', actual_types)

        all_pred_types.extend(pred_types)
        all_actual_types.extend(actual_types)

    unique, counts = np.unique(all_actual_types, return_counts=True)
    label_dist = dict(zip(unique, counts))

    # confusion matrix
    import pandas as pd
    import seaborn as sn
    from matplotlib import pyplot as plt

    all_possible_types = [ i + 1 for i in range(max(*all_actual_types, *all_pred_types)) ]
    actual_df = pd.Categorical(all_actual_types, categories=all_possible_types)
    predicted_df = pd.Categorical(all_pred_types, categories=[*all_possible_types, 'Totals'])
    cm = pd.crosstab(actual_df, predicted_df, rownames=['Actual'], colnames=['Predicted'])

    for idx in all_actual_types:
        if idx not in all_pred_types:
            cm[idx] = 0

    totals = [ sum(row) for (_, row) in cm.iterrows() ]
    cm['Totals'] = totals
    sorted_cols = sorted([ c for c in cm.columns if type(c) is int ])
    sorted_cols.append('Totals')
    cm = cm.reindex(sorted_cols, axis=1)

    sn.heatmap(cm, annot=True)
    plt.title(f'confusion matrix ({name})')
    plt.savefig(f'{logdir}/{name}/confusion_matrix.png')
    plt.clf()

    # save the model(s)
    model.save(f'{logdir}/{name}/model')

epoch_len = len(str(exp_config.epochs))
sample = None
for batch in loader_tr:
    preds, targets, loss, acc = train_step(*batch)

    tf.summary.scalar('loss', data=loss, step=iteration)
    tf.summary.scalar('accuracy', data=acc, step=iteration)

    model_loss += loss
    model_acc += acc
    current_batch += 1
    iteration += 1
    losses.append(loss)
    accuracies.append(acc)
    if current_batch == loader_tr.steps_per_epoch:
        model_loss /= loader_tr.steps_per_epoch
        model_acc /= loader_tr.steps_per_epoch
        epoch += 1

        # Compute validation loss and accuracy
        print('Ep. {} - Loss: {:.2f} - Acc: {:.2f}'.format(epoch, model_loss, model_acc))
        #val_loss, val_acc = evaluate(loader_va, [loss_fn, acc_fn])
        #print('Ep. {} - Loss: {:.2f} - Acc: {:.2f} - Val loss: {:.2f} - Val acc: {:.2f}'
              #.format(epoch, model_loss, model_acc, val_loss, val_acc))

        # Check if loss improved for early stopping
        if loss < best_val_loss:
            best_val_loss = loss
            print('New best loss {:.3f}'.format(loss))
            best_weights = model.get_weights()

        #if val_loss < best_val_loss:
            #best_val_loss = val_loss
            #patience = es_patience
            #print('New best val_loss {:.3f}'.format(val_loss))
            #best_weights = model.get_weights()
        #else:
            #patience -= 1
            #if patience == 0:
                #print('Early stopping (best val_loss: {})'.format(best_val_loss))
                #break
        #if sample is None:
            #sample = batch
        #action_probs, targets, _ = forward(model, *sample, training=False)
        # FIXME: uncomment this!
        # try:
            # print('sample', sample)
            # print('preds', preds)
            # print('action_probs', action_probs)
            # for (i, (pred, target)) in enumerate(zip(action_probs, targets)):
                # log_sample_prediction(i, epoch, pred, target)
        # except Exception as e:
            # raise e

        if exp_config.checkpoint_freq > 0 and epoch % exp_config.checkpoint_freq == 0:
            save_checkpoint(f'checkpoint_{str(epoch).zfill(epoch_len)}', model)

        model_loss = 0
        model_acc = 0
        current_batch = 0

has_saved_model = exp_config.checkpoint_freq > 0 and epoch % exp_config.checkpoint_freq == 0
if not has_saved_model:
    save_checkpoint(f'checkpoint_{str(epoch).zfill(epoch_len)}', model)

model.set_weights(best_weights)
save_checkpoint('best_model', model)

# Print summarization figures, stats
from matplotlib import pyplot as plt
plt.plot(accuracies)
plt.title('model accuracy')
plt.xlabel('epoch')
plt.ylabel('accuracy')
plt.legend(['train', 'val'])
plt.savefig(f'{logdir}/model_accuracy.png')

plt.clf()
plt.plot(losses)
plt.title('model loss')
plt.xlabel('epoch')
plt.ylabel('loss')
plt.legend(['train', 'val'])
plt.savefig(f'{logdir}/model_loss.png')
plt.clf()
