import numpy as np
import tensorflow as tf
import os
from tensorflow import layers
# import utils
from utils.data import Data
from utils.parameters import Parameters
from utils.image_embeddings import vgg16
from utils.caption_utils import preprocess_captions
# vae model
from model.decoder import Decoder
from ops import inference, optimizers

print("Tensorflow version: ", tf.__version__)


def main(params):
    # load data, class data contains captions, images, image features (if avaliable)
    if params.gen_val_captions < 0:
        repartiton = False
    else:
        repartiton = True
    data = Data(params, True, params.image_net_weights_path,
                repartiton=repartiton, gen_val_cap=params.gen_val_captions)
    # load batch generator, repartiton to use more val set images in train
    gen_batch_size = params.batch_size
    if params.fine_tune:
        gen_batch_size = params.batch_size
    batch_gen = data.load_train_data_generator(gen_batch_size,
                                               params.fine_tune)
    # whether use presaved pretrained imagenet features (saved in pickle)
    # feature extractor after fine_tune will be saved in tf checkpoint
    # caption generation after fine_tune must be made with params.fine_tune=True
    pretrained = not params.fine_tune
    val_gen = data.get_valid_data(gen_batch_size,
                                  val_tr_unused=batch_gen.unused_cap_in,
                                  pretrained=pretrained)
    test_gen = data.get_test_data(gen_batch_size,
                                  pretrained=pretrained)
    # annotations vector of form <EOS>...<BOS><PAD>...
    cap_enc = tf.placeholder(tf.int32, [None, None])
    cap_dec = tf.placeholder(tf.int32, [None, None])
    cap_len = tf.placeholder(tf.int32, [None])
    if params.fine_tune:
        # if fine_tune dont just load images_fv
        image_batch = tf.placeholder(tf.float32, [None, 224, 224, 3])
    else:
        # use prepared image features [batch_size, 4096] (fc2)
        image_batch = tf.placeholder(tf.float32, [None, 4096])
    if params.use_c_v or (
        params.prior == 'GMM' or params.prior == 'AG'):
        cl_vectors = tf.placeholder(tf.float32, [None, 90])
    else:
        cl_vectors = ann_lengths # dummy tensor
    # features, params.fine_tune stands for not using presaved imagenet weights
    # here, used this dummy placeholder during fine_tune
    # thats for saving image_net weights for futher usage
    image_f_inputs2 = tf.placeholder_with_default(
        tf.ones([1, 224, 224, 3]), shape=[None, 224, 224, 3], name='dummy_ps')
    if params.fine_tune:
        image_f_inputs2 = image_batch
    if params.mode == 'training' and params.fine_tune:
        cnn_dropout = params.cnn_dropout
        weights_regularizer = tf.contrib.layers.l2_regularizer(
            params.weight_decay)
    else:
        cnn_dropout = 1.0
        weights_regularizer = None
    with tf.variable_scope("cnn", regularizer=weights_regularizer):
        image_embeddings = vgg16(image_f_inputs2,
                                 trainable_fe=params.fine_tune_fe,
                                 trainable_top=params.fine_tune_top,
                                 dropout_keep=cnn_dropout)
    if params.fine_tune:
        features = image_embeddings.fc2
    else:
        features = image_batch
    # forward pass is expensive, so can use this method to reduce computation
    if params.num_captions > 1 and params.mode == 'training':  # [b_s, 4096]
        features_tiled = tf.tile(tf.expand_dims(features, 1),
                                 [1, params.num_captions, 1])
        features = tf.reshape(features_tiled,
                              [tf.shape(features)[0] * params.num_captions,
                               params.cnn_feature_size])  # [5 * b_s, 4096]
    # dictionary
    cap_dict = data.dictionary
    params.vocab_size = cap_dict.vocab_size
    # image features [b_size + f_size(4096)] -> [b_size + embed_size]
    images_fv = layers.dense(features, params.embed_size, name='imf_emb')
    # decoder, input_fv, get x, x_logits (for generation)
    decoder = Decoder(images_fv, cap_dec, cap_len, params,
                      cap_dict)
    if params.use_c_v:
        # cluster vectors from "Diverse and Accurate Image Description.." paper.
        c_i_emb = layers.dense(cl_vectors, params.embed_size, name='cv_emb')
        # map cluster vectors into embedding space
        decoder.c_i = c_i_emb
        decoder.c_i_ph = cl_vectors
    with tf.variable_scope("decoder"):
        x_logits,_ = decoder.decoder()
    # calculate rec. loss, mask padded part
    labels_flat = tf.reshape(cap_enc, [-1])
    ce_loss_padded = tf.nn.sparse_softmax_cross_entropy_with_logits(
        logits=x_logits, labels=labels_flat)
    loss_mask = tf.sign(tf.to_float(labels_flat))
    batch_loss = tf.div(tf.reduce_sum(tf.multiply(ce_loss_padded, loss_mask)),
                          tf.reduce_sum(loss_mask),
                          name="batch_loss")
    tf.losses.add_loss(batch_loss)
    rec_loss = tf.losses.get_total_loss()
    # overall loss reconstruction loss - kl_regularization
    # optimization, can print global norm for debugging
    optimize, global_step, global_norm = optimizers.non_cnn_optimizer(rec_loss,
                                                                      params)
    optimize_cnn = tf.constant(0.0)
    if params.fine_tune and params.mode == 'training':
        optimize_cnn, _ = optimizers.cnn_optimizer(lower_bound, params)
    # cnn parameters update
    # model restore
    vars_to_save = tf.trainable_variables()
    if not params.fine_tune_fe or not params.fine_tune_top:
        cnn_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, 'cnn')
        vars_to_save += cnn_vars
    saver = tf.train.Saver(vars_to_save,
                           max_to_keep=params.max_checkpoints_to_keep)
    # m_builder = tf.saved_model.builder.SavedModelBuilder('./saved_model')
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    with tf.Session(config=config) as sess:
        sess.run([tf.global_variables_initializer(),
                  tf.local_variables_initializer()])
        # train using batch generator, every iteration get
        # f(I), [batch_size, max_seq_len], seq_lengths
        if params.mode == "training":
            if params.logging:
                summary_writer = tf.summary.FileWriter(params.LOG_DIR,
                                                       sess.graph)
                summary_writer.add_graph(sess.graph)
            if not params.restore:
                print("Loading imagenet weights for futher usage")
                image_embeddings.load_weights(params.image_net_weights_path,
                                              sess)
            if params.restore:
                print("Restoring from checkpoint")
                saver.restore(sess, "./checkpoints/{}.ckpt".format(
                    params.checkpoint))
            for e in range(params.num_epochs):
                gs = tf.train.global_step(sess, global_step)
                gs_epoch = 0
                while True:
                    def stop_condition():
                        num_examples = gs_epoch * params.batch_size
                        if num_examples > params.num_ex_per_epoch:
                            return True
                        return False
                    for f_images_batch,\
                    captions_batch, cl_batch, c_v in batch_gen.next_batch(
                        use_obj_vectors=params.use_c_v,
                        num_captions=params.num_captions):
                        if params.num_captions > 1:
                            captions_batch, cl_batch, c_v = preprocess_captions(
                                captions_batch, cl_batch, c_v)
                        feed = {image_f_inputs: f_images_batch,
                                cap_enc: captions_batch[1],
                                cap_dec: captions_batch[0],
                                cap_len: cl_batch
                                }
                        if params.use_c_v:
                            feed.update({c_i: c_v[:, 1:]})
                        gs = tf.train.global_step(sess, global_step)
                        feed.update({anneal: gs})
                        # print(sess.run(debug_print, feed))
                        total_loss_ ,_,_ = sess.run([rec_loss, optimize,
                                                optimize_cnn], feed)
                        gs_epoch += 1
                        if gs % 500 == 0:
                            print("Total training loss: {} iteraton: {}".format(
                                total_loss_, gs))
                        if stop_condition():
                            break
                    if stop_condition():
                        break
                print("Epoch: {} training loss {}".format(e, total_loss_))
                def validate():
                    val_rec = []
                    for f_images_batch, captions_batch, cl_batch, c_v in val_gen.next_batch(
                        use_obj_vectors=params.use_c_v,
                        num_captions=params.num_captions):
                        gs = tf.train.global_step(sess, global_step)
                        if params.num_captions > 1:
                            captions_batch, cl_batch, c_v= preprocess_captions(
                                captions_batch, cl_batch,c_v)
                        feed = {image_f_inputs: f_images_batch,
                                cap_enc: captions_batch[1],
                                cap_dec: captions_batch[0],
                                cap_len: cl_batch}
                        if params.use_c_v:
                            feed.update({c_i: c_v[:, 1:]})
                        rl = sess.run([rec_loss], feed_dict=feed)
                        val_rec.append(rl)
                    print("Validation reconstruction loss: {}".format(
                        np.mean(val_rec)))
                    print("-----------------------------------------------")
                validate()
                # save model
                if not os.path.exists("./checkpoints"):
                    os.makedirs("./checkpoints")
                save_path = saver.save(sess, "./checkpoints/{}.ckpt".format(
                    params.checkpoint))
                print("Model saved in file: %s" % save_path)
        # builder.add_meta_graph_and_variables(sess, ["main_model"])
        if params.use_hdf5 and params.fine_tune:
            batch_gen.h5f.close()
        # run inference
        if params.mode == "inference":
            inference.inference(params, decoder, val_gen,
                                test_gen, image_f_inputs, saver, sess)


if __name__ == '__main__':
    params = Parameters()
    params.parse_args()
    coco_dir = params.coco_dir
    # save parameters for futher usage
    if params.save_params:
        import pickle
        param_fn = "./pickles/params_{}_{}_{}_{}.pickle".format(params.prior,
                                        params.no_encoder,
                                        params.checkpoint,
                                        params.use_c_v)
        print("Saving params to: ", param_fn)
        with open(param_fn, 'wb') as wf:
            pickle.dump(file=wf, obj=params)
    # train model, generate captions for val-test sets
    main(params)
