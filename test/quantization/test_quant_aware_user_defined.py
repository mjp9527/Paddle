# Copyright (c) 2019  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import unittest

import numpy as np
from test_quant_aware import MobileNet, StaticCase

import paddle
from paddle.static.quantization.quanter import convert, quant_aware


def pact(x):
    helper = paddle.fluid.layer_helper.LayerHelper("pact", **locals())
    dtype = 'float32'
    init_thres = 20
    u_param_attr = paddle.ParamAttr(
        name=x.name + '_pact',
        initializer=paddle.nn.initializer.Constant(value=init_thres),
        regularizer=paddle.regularizer.L2Decay(0.0001),
        learning_rate=1,
    )
    u_param = helper.create_parameter(attr=u_param_attr, shape=[1], dtype=dtype)

    part_a = paddle.nn.functional.relu(x - u_param)
    part_b = paddle.nn.functional.relu(-u_param - x)
    x = x - part_a + part_b
    return x


def get_optimizer():
    return paddle.optimizer.Momentum(0.0001, 0.9)


class TestQuantAwareCase1(StaticCase):
    def get_model(self):
        image = paddle.static.data(
            name='image', shape=[None, 1, 28, 28], dtype='float32'
        )
        label = paddle.static.data(name='label', shape=[None, 1], dtype='int64')
        model = MobileNet()
        out = model.net(input=image, class_dim=10)
        cost = paddle.nn.functional.loss.cross_entropy(input=out, label=label)
        avg_cost = paddle.mean(x=cost)
        startup_prog = paddle.static.default_startup_program()
        train_prog = paddle.static.default_main_program()
        return startup_prog, train_prog

    def test_accuracy(self):
        image = paddle.static.data(
            name='image', shape=[None, 1, 28, 28], dtype='float32'
        )
        image.stop_gradient = False
        label = paddle.static.data(name='label', shape=[None, 1], dtype='int64')
        model = MobileNet()
        out = model.net(input=image, class_dim=10)
        cost = paddle.nn.functional.loss.cross_entropy(input=out, label=label)
        avg_cost = paddle.mean(x=cost)
        acc_top1 = paddle.metric.accuracy(input=out, label=label, k=1)
        acc_top5 = paddle.metric.accuracy(input=out, label=label, k=5)
        optimizer = paddle.optimizer.Momentum(
            momentum=0.9,
            learning_rate=0.01,
            weight_decay=paddle.regularizer.L2Decay(4e-5),
        )
        optimizer.minimize(avg_cost)
        main_prog = paddle.static.default_main_program()
        val_prog = main_prog.clone(for_test=True)

        place = (
            paddle.CUDAPlace(0)
            if paddle.is_compiled_with_cuda()
            else paddle.CPUPlace()
        )
        exe = paddle.static.Executor(place)
        exe.run(paddle.static.default_startup_program())

        def transform(x):
            return np.reshape(x, [1, 28, 28])

        train_dataset = paddle.vision.datasets.MNIST(
            mode='train', backend='cv2', transform=transform
        )
        test_dataset = paddle.vision.datasets.MNIST(
            mode='test', backend='cv2', transform=transform
        )
        batch_size = 64 if os.environ.get('DATASET') == 'full' else 8
        train_loader = paddle.io.DataLoader(
            train_dataset,
            places=place,
            feed_list=[image, label],
            drop_last=True,
            return_list=False,
            batch_size=batch_size,
        )
        valid_loader = paddle.io.DataLoader(
            test_dataset,
            places=place,
            feed_list=[image, label],
            batch_size=batch_size,
            return_list=False,
        )

        def train(program):
            iter = 0
            stop_iter = None if os.environ.get('DATASET') == 'full' else 10
            for data in train_loader():
                cost, top1, top5 = exe.run(
                    program,
                    feed=data,
                    fetch_list=[avg_cost, acc_top1, acc_top5],
                )
                iter += 1
                if iter % 100 == 0:
                    print(
                        'train iter={}, avg loss {}, acc_top1 {}, acc_top5 {}'.format(
                            iter, cost, top1, top5
                        )
                    )
                if stop_iter is not None and iter == stop_iter:
                    break

        def test(program):
            iter = 0
            stop_iter = None if os.environ.get('DATASET') == 'full' else 10
            result = [[], [], []]
            for data in valid_loader():
                cost, top1, top5 = exe.run(
                    program,
                    feed=data,
                    fetch_list=[avg_cost, acc_top1, acc_top5],
                )
                iter += 1
                if iter % 100 == 0:
                    print(
                        'eval iter={}, avg loss {}, acc_top1 {}, acc_top5 {}'.format(
                            iter, cost, top1, top5
                        )
                    )
                result[0].append(cost)
                result[1].append(top1)
                result[2].append(top5)
                if stop_iter is not None and iter == stop_iter:
                    break
            print(
                ' avg loss {}, acc_top1 {}, acc_top5 {}'.format(
                    np.mean(result[0]), np.mean(result[1]), np.mean(result[2])
                )
            )
            return np.mean(result[1]), np.mean(result[2])

        train(main_prog)
        top1_1, top5_1 = test(main_prog)

        config = {
            'weight_quantize_type': 'channel_wise_abs_max',
            'activation_quantize_type': 'moving_average_abs_max',
            'quantize_op_types': ['depthwise_conv2d', 'mul', 'conv2d'],
            'onnx_format': False,
        }
        quant_train_prog_pact = quant_aware(
            main_prog,
            place,
            config,
            for_test=False,
            act_preprocess_func=pact,
            optimizer_func=get_optimizer,
            executor=exe,
        )

        quant_eval_prog = quant_aware(val_prog, place, config, for_test=True)
        train(quant_train_prog_pact)
        quant_eval_prog = convert(quant_eval_prog, place, config)
        top1_2, top5_2 = test(quant_eval_prog)
        # values before quantization and after quantization should be close
        print(f"before quantization: top1: {top1_1}, top5: {top5_1}")
        print(f"after quantization: top1: {top1_2}, top5: {top5_2}")


if __name__ == '__main__':
    unittest.main()
