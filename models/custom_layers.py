import tensorflow as tf
from tensorflow.keras import layers


class StandardizeImage(layers.Layer):

    def call(self, inputs, **kwargs):
        tf.debugging.assert_rank(inputs, 4)

        MEAN_RGB = [0.485 * 255, 0.456 * 255, 0.406 * 255]
        STDDEV_RGB = [0.229 * 255, 0.224 * 255, 0.225 * 255]
        inputs -= tf.constant(MEAN_RGB, shape=[1, 1, 1, 3], dtype=self.dtype)
        inputs /= tf.constant(STDDEV_RGB, shape=[1, 1, 1, 3], dtype=self.dtype)
        return inputs


class FeatViews(layers.Layer):
    def call(self, inputs, **kwargs):
        feats1, feats2 = inputs
        feats1 = tf.expand_dims(feats1, axis=1)
        feats2 = tf.expand_dims(feats2, axis=1)
        feat_views = tf.concat([feats1, feats2], axis=1)
        return feat_views


class GlobalBatchSims(layers.Layer):
    def call(self, inputs, **kwargs):
        feats1, feats2 = inputs

        if tf.distribute.in_cross_replica_context():
            strategy = tf.distribute.get_strategy()
            global_feats2 = strategy.gather(feats2, axis=0)
        else:
            replica_context = tf.distribute.get_replica_context()
            global_feats2 = replica_context.all_gather(feats2, axis=0)
        feat_sims = tf.matmul(feats1, global_feats2, transpose_b=True)
        return feat_sims


class L2Normalize(layers.Layer):
    def call(self, inputs, **kwargs):
        tf.debugging.assert_rank(inputs, 2)
        return tf.nn.l2_normalize(inputs, axis=1)


class CastFloat32(layers.Layer):
    def call(self, inputs, **kwargs):
        return tf.cast(inputs, tf.float32)


custom_objects = {
    'StandardizeImage': StandardizeImage,
    'FeatViews': FeatViews,
    'GlobalBatchSims': GlobalBatchSims,
    'L2Normalize': L2Normalize,
    'CastFloat32': CastFloat32
}
