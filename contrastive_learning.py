# -*- coding: utf-8 -*-
"""contrastive learning.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/186x4ArfFLbhkROefChnTbvdBzWXXMGau

## Setup
"""

import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import applications, layers, losses, metrics, mixed_precision, datasets

import os
import numpy as np
from tqdm.auto import tqdm

import argparse

parser = argparse.ArgumentParser()

parser.add_argument('--epochs', type=int)
parser.add_argument('--bsz', type=int)

parser.add_argument('--lr', type=float)
parser.add_argument('--load', action='store_true')

parser.add_argument('--method', choices=['ce', 'supcon', 'supcon-pce'])

parser.add_argument('--out', type=str, default='./')

"""## Data"""

from tensorflow.data import AUTOTUNE

class Augment(layers.Layer):
  def __init__(self, imsize, rand_crop, rand_flip, rand_jitter, rand_gray):
    super().__init__(name='image-augmentation')
    self.imsize = imsize
    self.rand_crop = rand_crop
    self.rand_flip = rand_flip
    self.rand_jitter = rand_jitter
    self.rand_gray = rand_gray

  @tf.function
  def call(self, image):
    # Convert to float
    image = tf.image.convert_image_dtype(image, tf.float32)

    # Crop
    if self.rand_crop:
      rand_scale = tf.random.uniform([], 1, 2)
      rand_size = tf.round(rand_scale * self.imsize)
      image = tf.image.resize(image, [rand_size, rand_size])
      image = tf.image.random_crop(image, [self.imsize, self.imsize, 3])
    else:
      image = tf.image.resize(image, [self.imsize, self.imsize])
    
    # Random flip
    if self.rand_flip:
      image = tf.image.random_flip_left_right(image)
    
    # Color Jitter
    if self.rand_jitter and tf.random.uniform([]) < 0.8:
      image = tf.image.random_brightness(image, 0.4)
      image = tf.image.random_contrast(image, 0.6, 1.4)
      image = tf.image.random_saturation(image, 0.6, 1.4)
      image = tf.image.random_hue(image, 0.1)
    
    # Gray scale
    if self.rand_gray and tf.random.uniform([]) < 0.2:
      image = tf.image.rgb_to_grayscale(image)
      image = tf.tile(image, [1, 1, 3])
    
    # Clip
    image = tf.clip_by_value(image, 0, 1)
    
    return image

def load_datasets(args, strategy):
  (x_train, y_train), (x_test, y_test) = datasets.cifar10.load_data()
  ds_train = tf.data.Dataset.from_tensor_slices((x_train, y_train.flatten()))
  ds_test = tf.data.Dataset.from_tensor_slices((x_test, y_test.flatten()))
  
  augment = Augment(imsize=32, rand_crop=True, rand_flip=True, 
                    rand_jitter=True, rand_gray=True)
  def train_map(imgs, labels):
    return augment(imgs), labels

  ds_train = (
      ds_train
      .cache()
      .map(train_map, num_parallel_calls=AUTOTUNE)
      .shuffle(len(ds_train))
      .batch(args.bsz, drop_remainder=True)
      .prefetch(AUTOTUNE)
  )
  ds_test = (
      ds_test
      .cache()
      .shuffle(len(ds_test))
      .batch(args.bsz)
      .prefetch(AUTOTUNE)
  )

  ds_train = strategy.experimental_distribute_dataset(ds_train)
  ds_test = strategy.experimental_distribute_dataset(ds_test)

  return ds_train, ds_test

"""## Loss"""

@tf.function
def supcon_loss(labels, feats1, feats2, partial):  
  tf.debugging.assert_all_finite(feats1, 'feats1 not finite')
  tf.debugging.assert_all_finite(feats2, 'feats2 not finite')
  bsz = len(labels)
  labels = tf.expand_dims(labels, 1)

  # Masks
  inst_mask = tf.eye(bsz, dtype=tf.float16)
  class_mask = tf.cast(labels == tf.transpose(labels), tf.float16)
  class_sum = tf.math.reduce_sum(class_mask, axis=1, keepdims=True)

  # Similarities
  sims = tf.matmul(feats1, tf.transpose(feats2))
  tf.debugging.assert_all_finite(sims, 'similarities not finite')
  tf.debugging.assert_less_equal(sims, tf.ones_like(sims) + 1e-2, 
                                 'similarities not less than or equal to 1')

  if partial:
    # Partial cross entropy
    pos_mask = tf.maximum(inst_mask, class_mask)
    neg_mask = 1 - pos_mask

    exp = tf.math.exp(sims * 10)
    neg_sum_exp = tf.math.reduce_sum(exp * neg_mask, axis=1, keepdims=True)
    log_prob = sims - tf.math.log(neg_sum_exp + exp)

    # Class positive pairs log prob (contains instance positive pairs too)
    class_log_prob = class_mask * log_prob
    class_log_prob = tf.math.reduce_sum(class_log_prob / class_sum, axis=1)

    loss = -class_log_prob
  else:
    # Cross entropy
    loss = losses.categorical_crossentropy(class_mask / class_sum, sims * 10, 
                                           from_logits=True)    
  return loss

"""## Model"""

class ContrastModel(keras.Model):
  def __init__(self, args):
    super().__init__()
    
    self.cnn = applications.ResNet50V2(weights=None, include_top=False) 
    self.avg_pool = layers.GlobalAveragePooling2D()
    self.proj_w = layers.Dense(128, name='projection')
    self.classifier = layers.Dense(10, name='classifier')

    if args.load:
      print(f'loaded previously saved model weights')
      self.load_weights(os.path.join(args.out, 'model'))
    else:
      print(f'starting with new model weights')

  def feats(self, img):
    x = applications.resnet_v2.preprocess_input(img)
    x = self.cnn(img)
    x = self.avg_pool(x)
    x, _ = tf.linalg.normalize(x, axis=-1)
    return x

  def project(self, feats):
    x = self.proj_w(feats)
    x, _ = tf.linalg.normalize(x, axis=-1)
    return x
  
  def call(self, img):
    feats = self.feats(img)
    proj = self.project(feats)
    return self.classifier(feats), proj

  @tf.function
  def train_step(self, method, bsz, imgs1, labels):
    with tf.GradientTape() as tape:
      if method.startswith('supcon'):
        partial = method.endswith('pce')

        # Features
        feats1 = self.feats(imgs1)
        proj1 = self.project(feats1)

        # Contrast
        con_loss = supcon_loss(labels, proj1, tf.stop_gradient(proj1), partial)
        con_loss = tf.nn.compute_average_loss(con_loss, global_batch_size=bsz)

        pred_logits = self.classifier(tf.stop_gradient(feats1))
      elif method == 'ce':
        con_loss = 0
        pred_logits, _ = self(imgs1)
      else:
        raise Exception(f'unknown train method {method}')

      # Classifer cross entropy
      class_loss = losses.sparse_categorical_crossentropy(labels, pred_logits, 
                                                          from_logits=True)
      class_loss = tf.nn.compute_average_loss(class_loss, global_batch_size=bsz)
      loss = con_loss + class_loss
      tf.debugging.assert_all_finite(loss, 'loss not finite')
      scaled_loss = self.optimizer.get_scaled_loss(loss)

    # Gradient descent
    scaled_gradients = tape.gradient(scaled_loss, self.trainable_variables)
    gradients = self.optimizer.get_unscaled_gradients(scaled_gradients)
    self.optimizer.apply_gradients(zip(gradients, self.trainable_weights))

    # Accuracy
    acc = metrics.sparse_categorical_accuracy(labels, pred_logits)
    acc = tf.nn.compute_average_loss(acc, global_batch_size=bsz)
    return loss, acc

  @tf.function
  def test_step(self, bsz, imgs1, labels):
    imgs1 = tf.image.convert_image_dtype(imgs1, tf.float32)
    pred_logits, _ = self(imgs1)

    acc = metrics.sparse_categorical_accuracy(labels, pred_logits)
    acc = tf.nn.compute_average_loss(acc, global_batch_size=bsz)
    return acc

"""## Train"""

def epoch_train(args, model, strategy, ds_train):
  accs, losses = [], []
  for imgs1, labels in tqdm(ds_train, 'train', leave=False, mininterval=2):
    # Train step
    loss, acc = strategy.run(model.train_step, 
                             args=(args.method, args.bsz, imgs1, labels))
    loss = strategy.reduce('SUM', loss, axis=None)
    acc = strategy.reduce('SUM', acc, axis=None)

    # Record
    losses.append(float(loss))
    accs.append(float(acc))
    
  return accs, losses

def epoch_test(args, model, strategy, ds_test):
  accs = []
  for imgs1, labels in tqdm(ds_test, 'test', leave=False, mininterval=2):
    # Train step
    acc = strategy.run(model.test_step, args=(args.bsz, imgs1, labels))
    acc = strategy.reduce('SUM', acc, axis=None)

    # Record
    accs.append(float(acc))
  return accs

def train(args, model, strategy, ds_train, ds_test):
  all_train_accs, all_train_losses = [], []
  test_accs = []

  try:
    pbar = tqdm(range(args.epochs), 'epochs', mininterval=2)
    for epoch in pbar:
      # Train
      train_accs, train_losses = epoch_train(args, model, strategy, ds_train)
      model.save_weights(os.path.join(args.out, 'model'))
      all_train_accs.append(train_accs)
      all_train_losses.append(train_losses)

      # Test
      test_accs = epoch_test(args, model, strategy, ds_test)
      pbar.set_postfix_str(f'{np.mean(train_losses):.3} loss, ' \
                           f'{np.mean(train_accs):.3} acc, ' \
                           f'{np.mean(test_accs):.3} test acc', refresh=False)
  except KeyboardInterrupt:
    print('keyboard interrupt caught. ending training early')

  return test_accs, all_train_accs, all_train_losses

"""## Plot"""

import matplotlib.pyplot as plt

def plot_img_samples(args, ds_train, ds_test):
  f, ax = plt.subplots(2, 8)
  f.set_size_inches(20, 6)
  for i, ds in enumerate([ds_train, ds_test]):
    imgs = next(iter(ds))[0]
    for j in range(8):
      ax[i, j].set_title('train' if i == 0 else 'test')
      ax[i, j].imshow(imgs[j])
    
  f.tight_layout()
  f.savefig(os.path.join(args.out, 'img-samples.jpg'))
  plt.show()

def plot_tsne(args, model, ds_test):
  from sklearn import manifold
  
  all_feats, all_proj, all_labels = [], [], []
  for imgs, labels in ds_test:
    imgs = tf.image.convert_image_dtype(imgs, tf.float32)
    feats = model.feats(imgs)
    proj = model.project(feats)
    
    all_feats.append(feats.numpy())
    all_proj.append(proj.numpy())
    all_labels.append(labels.numpy())
  
  all_feats = np.concatenate(all_feats)
  all_proj = np.concatenate(all_proj)
  all_labels = np.concatenate(all_labels)

  feats_embed = manifold.TSNE().fit_transform(all_feats)
  proj_embed = manifold.TSNE().fit_transform(all_proj)

  classes = np.unique(all_labels)
  f, ax = plt.subplots(1, 2)
  f.set_size_inches(13, 5)
  ax[0].set_title('feats')
  ax[1].set_title('projected features')
  for c in classes:
    class_feats_embed = feats_embed[all_labels == c]
    class_proj_embed = proj_embed[all_labels == c]

    ax[0].scatter(class_feats_embed[:, 0], class_feats_embed[:, 1], label=f'{c}')
    ax[1].scatter(class_proj_embed[:, 0], class_proj_embed[:, 1], label=f'{c}')

  f.savefig(os.path.join(args.out, 'tsne.jpg'))
  plt.show()

def plot_metrics(args, metrics):
  f, ax = plt.subplots(1, len(metrics))
  f.set_size_inches(15, 5)

  names = ['test accs', 'train accs', 'train losses']
  for i, (y, name) in enumerate(zip(metrics, names)):
    y = np.array(y)
    x = np.linspace(0, len(y), y.size)
    ax[i].set_title(name)
    ax[i].set_xlabel('epochs')
    ax[i].plot(x, y.flatten())
  
  f.savefig(os.path.join(args.out, 'metrics.jpg'))
  plt.show()

"""## Main"""

def run(args):
  # Mixed precision
  policy = mixed_precision.Policy('mixed_float16')  
  mixed_precision.set_global_policy(policy)

  # Strategy
  gpus = tf.config.list_physical_devices('GPU')
  print(f'GPUs: {gpus}')
  if len(gpus) > 1:
    strategy = tf.distribute.MirroredStrategy()
  else:
    strategy = tf.distribute.get_strategy() 

  # Data
  ds_train, ds_test = load_datasets(args, strategy)
  if len(gpus) <= 1:
    plot_img_samples(args, ds_train, ds_test)

  # Model and optimizer
  with strategy.scope():
    model = ContrastModel(args)
    opt = keras.optimizers.SGD(args.lr, momentum=0.9)
    model.optimizer = mixed_precision.LossScaleOptimizer(opt)
    
  # Train
  metrics = train(args, model, strategy, ds_train, ds_test)

  # Plot
  plot_metrics(args, metrics)
  plot_tsne(args, model, ds_test)

args = '--bsz=1024 --epochs=10 --method=supcon --lr=1e-3'
args = parser.parse_args(args.split())
print(args)

run(args)

# Commented out IPython magic to ensure Python compatibility.
# %debug

