import os
import shutil

import tensorflow as tf
from tensorflow.keras import callbacks, optimizers

from models import custom_losses


def train(args, model, ds_train, ds_val, ds_info):
    # Output
    if not args.load:
        if args.out.startswith('gs://'):
            os.system(f"gsutil -m rm {os.path.join(args.out, '**')}")
        else:
            shutil.rmtree(args.out)
            os.mkdir(args.out)

    # Callbacks
    cbks = get_callbacks(args)

    try:
        train_steps, val_steps = args.train_steps, args.val_steps
        if args.steps_exec is not None:
            ds_train, ds_val = ds_train.repeat(), ds_val.repeat()
            if args.train_steps is None:
                train_steps = ds_info['train_size'] // args.bsz
                print('steps per execution set and train_steps not specified. '
                      f'setting it to train_size // bsz = {train_steps}')
            if args.val_steps is None:
                val_steps = ds_info['val_size'] // args.bsz
                print('steps per execution set and val_steps not specified. '
                      f'setting it to val_size // bsz = {val_steps}')

        model.fit(ds_train, initial_epoch=args.init_epoch, epochs=args.epochs,
                  validation_data=ds_val, validation_steps=val_steps, steps_per_epoch=train_steps,
                  callbacks=cbks)
    except KeyboardInterrupt:
        print('keyboard interrupt caught. ending training early')


def get_callbacks(args):
    cbks = []

    # Save work?
    if not args.no_save:
        cbks.append(callbacks.TensorBoard(os.path.join(args.out, 'logs'), histogram_freq=1,
                                          update_freq=args.update_freq, write_graph=False))
        cbks.append(callbacks.ModelCheckpoint(os.path.join(args.out, 'model'), verbose=1,
                                              save_best_only=True, monitor='val_loss', mode='min'))

    # Learning rate schedule
    def scheduler(epoch, _):
        curr_lr = args.lr
        if args.lr_decays is None:
            return curr_lr

        for e in range(epoch + 1):
            if e in args.lr_decays:
                curr_lr *= 0.1
        return curr_lr

    cbks.append(callbacks.LearningRateScheduler(scheduler, verbose=1))
    return cbks


def compile_model(args, model):
    # Optimizer
    if args.optimizer == 'sgd':
        opt = optimizers.SGD(args.lr, momentum=0.9)
    elif args.optimizer == 'adam':
        opt = optimizers.Adam(args.lr)
    else:
        raise Exception(f'unknown optimizer {args.optimizer}')
    if args.debug:
        print(f'{opt} optimizer')

    # Loss and metrics
    losses = {'labels': tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)}
    metrics = {'labels': 'acc'}

    contrast_loss_dict = {
        'supcon': custom_losses.SupCon(),
        'partial-supcon': custom_losses.PartialSupCon(),
        'simclr': custom_losses.SimCLR(),
        'no-op': custom_losses.NoOp()
    }
    if args.loss in contrast_loss_dict:
        losses['contrast'] = contrast_loss_dict[args.loss]
        if not args.model.endswith('-norm'):
            print('WARNING: Optimizing over contrastive loss without l2 normalization')

    # Compile
    model.compile(opt, losses, metrics, steps_per_execution=args.steps_exec)
