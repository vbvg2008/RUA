import math
import random
import tempfile

import fastestimator as fe
import numpy as np
import tensorflow as tf
import torch
import torch.nn as nn
import torch.nn.functional as F
from fastestimator.op.numpyop import NumpyOp
from fastestimator.op.numpyop.meta import Sometimes
from fastestimator.op.numpyop.multivariate import HorizontalFlip, PadIfNeeded, RandomCrop
from fastestimator.op.numpyop.univariate import ChannelTranspose, CoarseDropout, Normalize
from fastestimator.op.tensorop.loss import CrossEntropy
from fastestimator.op.tensorop.model import ModelOp, UpdateOp
from fastestimator.schedule import cosine_decay
from fastestimator.trace.adapt import LRScheduler
from fastestimator.trace.io import BestModelSaver
from fastestimator.trace.metric import Accuracy
from PIL import Image, ImageEnhance, ImageOps, ImageTransform
from sklearn.model_selection import train_test_split


class BasicBlock(nn.Module):
    def __init__(self, in_planes, out_planes, stride, dropRate=0.0):
        super(BasicBlock, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.relu1 = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(out_planes)
        self.relu2 = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv2d(out_planes, out_planes, kernel_size=3, stride=1, padding=1, bias=False)
        self.droprate = dropRate
        self.equalInOut = (in_planes == out_planes)
        self.convShortcut = (not self.equalInOut) and nn.Conv2d(
            in_planes, out_planes, kernel_size=1, stride=stride, padding=0, bias=False) or None

    def forward(self, x):
        if not self.equalInOut:
            x = self.relu1(self.bn1(x))
        else:
            out = self.relu1(self.bn1(x))
        out = self.relu2(self.bn2(self.conv1(out if self.equalInOut else x)))
        if self.droprate > 0:
            out = F.dropout(out, p=self.droprate, training=self.training)
        out = self.conv2(out)
        return torch.add(x if self.equalInOut else self.convShortcut(x), out)


class NetworkBlock(nn.Module):
    def __init__(self, nb_layers, in_planes, out_planes, block, stride, dropRate=0.0):
        super(NetworkBlock, self).__init__()
        self.layer = self._make_layer(block, in_planes, out_planes, nb_layers, stride, dropRate)

    def _make_layer(self, block, in_planes, out_planes, nb_layers, stride, dropRate):
        layers = []
        for i in range(int(nb_layers)):
            layers.append(block(i == 0 and in_planes or out_planes, out_planes, i == 0 and stride or 1, dropRate))
        return nn.Sequential(*layers)

    def forward(self, x):
        return self.layer(x)


class WideResNet(nn.Module):
    def __init__(self, depth, num_classes, widen_factor=1, dropRate=0.0):
        super(WideResNet, self).__init__()
        nChannels = [16, 16 * widen_factor, 32 * widen_factor, 64 * widen_factor]
        assert ((depth - 4) % 6 == 0)
        n = (depth - 4) / 6
        block = BasicBlock
        # 1st conv before any network block
        self.conv1 = nn.Conv2d(3, nChannels[0], kernel_size=3, stride=1, padding=1, bias=False)
        # 1st block
        self.block1 = NetworkBlock(n, nChannels[0], nChannels[1], block, 1, dropRate)
        # 2nd block
        self.block2 = NetworkBlock(n, nChannels[1], nChannels[2], block, 2, dropRate)
        # 3rd block
        self.block3 = NetworkBlock(n, nChannels[2], nChannels[3], block, 2, dropRate)
        # global average pooling and classifier
        self.bn1 = nn.BatchNorm2d(nChannels[3])
        self.relu = nn.ReLU(inplace=True)
        self.fc = nn.Linear(nChannels[3], num_classes)
        self.nChannels = nChannels[3]

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                m.bias.data.zero_()

    def forward(self, x):
        out = self.conv1(x)
        out = self.block1(out)
        out = self.block2(out)
        out = self.block3(out)
        out = self.relu(self.bn1(out))
        out = F.avg_pool2d(out, 8, 1)
        out = out.view(-1, self.nChannels)
        return self.fc(out)


class Rotate(NumpyOp):
    """ rotate between 0 to 90 degree
    """
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.degree = level * 3.0

    def forward(self, data, state):
        im = Image.fromarray(data)
        degree = random.uniform(-self.degree, self.degree)
        im = im.rotate(degree)
        return np.copy(np.asarray(im))


class AutoContrast(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)

    def forward(self, data, state):
        im = Image.fromarray(data)
        im = ImageOps.autocontrast(im)
        return np.copy(np.asarray(im))


class Equalize(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)

    def forward(self, data, state):
        im = Image.fromarray(data)
        im = ImageOps.equalize(im)
        return np.copy(np.asarray(im))


class Posterize(NumpyOp):
    # resuce the number of bits for each channel, this may be inconsistent with original implementation
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.bit_loss_limit = level / 30 * 7

    def forward(self, data, state):
        im = Image.fromarray(data)
        bits_to_keep = 8 - round(random.uniform(0, self.bit_loss_limit))
        im = ImageOps.posterize(im, bits_to_keep)
        return np.copy(np.asarray(im))


class Solarize(NumpyOp):
    # this may be inconsistent with original implementation
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.loss_limit = level / 30 * 256

    def forward(self, data, state):
        threshold = 256 - round(random.uniform(0, self.loss_limit))
        data = np.where(data < threshold, data, 255 - data)
        return data


class Sharpness(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.diff_limit = level / 30 * 0.9

    def forward(self, data, state):
        im = Image.fromarray(data)
        factor = 1.0 + random.uniform(-self.diff_limit, self.diff_limit)
        im = ImageEnhance.Sharpness(im).enhance(factor)
        return np.copy(np.asarray(im))


class Contrast(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.diff_limit = level / 30 * 0.9

    def forward(self, data, state):
        im = Image.fromarray(data)
        factor = 1.0 + random.uniform(-self.diff_limit, self.diff_limit)
        im = ImageEnhance.Contrast(im).enhance(factor)
        return np.copy(np.asarray(im))


class Color(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.diff_limit = level / 30 * 0.9

    def forward(self, data, state):
        im = Image.fromarray(data)
        factor = 1.0 + random.uniform(-self.diff_limit, self.diff_limit)
        im = ImageEnhance.Color(im).enhance(factor)
        return np.copy(np.asarray(im))


class Brightness(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.diff_limit = level / 30 * 0.9

    def forward(self, data, state):
        im = Image.fromarray(data)
        factor = 1.0 + random.uniform(-self.diff_limit, self.diff_limit)
        im = ImageEnhance.Brightness(im).enhance(factor)
        return np.copy(np.asarray(im))


class ShearX(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.shear_coef = level / 30 * 0.5

    def forward(self, data, state):
        im = Image.fromarray(data)
        shear_coeff = random.uniform(-self.shear_coef, self.shear_coef)
        width, height = im.size
        xshift = round(abs(shear_coeff) * width)
        new_width = width + xshift
        im = im.transform((new_width, height),
                          ImageTransform.AffineTransform(
                              (1.0, shear_coeff, -xshift if shear_coeff > 0 else 0.0, 0.0, 1.0, 0.0)),
                          resample=Image.BICUBIC)
        im = im.resize((width, height))
        return np.copy(np.asarray(im))


class ShearY(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.shear_coef = level / 30 * 0.5

    def forward(self, data, state):
        im = Image.fromarray(data)
        shear_coeff = random.uniform(-self.shear_coef, self.shear_coef)
        width, height = im.size
        yshift = round(abs(shear_coeff) * height)
        newheight = height + yshift
        im = im.transform((width, newheight),
                          ImageTransform.AffineTransform(
                              (1.0, 0.0, 0.0, shear_coeff, 1.0, -yshift if shear_coeff > 0 else 0.0)),
                          resample=Image.BICUBIC)
        im = im.resize((width, height))
        return np.copy(np.asarray(im))


class TranslateX(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.level = level

    def forward(self, data, state):
        im = Image.fromarray(data)
        width, height = im.size
        displacement = random.uniform(-self.level / 30 * height / 3, self.level / 30 * height / 3)
        im = im.transform((width, height),
                          ImageTransform.AffineTransform((1.0, 0.0, displacement, 0.0, 1.0, 0.0)),
                          resample=Image.BICUBIC)
        return np.copy(np.asarray(im))


class TranslateY(NumpyOp):
    def __init__(self, level, inputs=None, outputs=None, mode=None):
        super().__init__(inputs=inputs, outputs=outputs, mode=mode)
        self.level = level

    def forward(self, data, state):
        im = Image.fromarray(data)
        width, height = im.size
        displacement = random.uniform(-self.level / 30 * height / 3, self.level / 30 * height / 3)
        im = im.transform((width, height),
                          ImageTransform.AffineTransform((1.0, 0.0, 0.0, 0.0, 1.0, displacement)),
                          resample=Image.BICUBIC)
        return np.copy(np.asarray(im))


def get_probability(N, P):
    """
    N: number of augmentation
    P: the probability of having at least 1 augmentation

    return: the N needed
    """
    return 1 - math.exp(math.log(1 - P) / N)


def get_estimator(level, epochs=200, batch_size=128, save_dir=tempfile.mkdtemp()):
    print("trying level {}".format(level))
    # step 1: prepare dataset
    (x_train, y_train), (_, _) = tf.keras.datasets.cifar10.load_data()
    x_train, x_eval, y_train, y_eval = train_test_split(x_train, y_train, test_size=0.1, random_state=24, stratify=y_train)
    train_data = fe.dataset.NumpyDataset({"x": x_train, "y": y_train})
    eval_data = fe.dataset.NumpyDataset({"x": x_eval, "y": y_eval})
    aug_options = [
        Rotate(level=level, inputs="x", outputs="x", mode="train"),
        AutoContrast(level=level, inputs="x", outputs="x", mode="train"),
        Equalize(level=level, inputs="x", outputs="x", mode="train"),
        Posterize(level=level, inputs="x", outputs="x", mode="train"),
        Solarize(level=level, inputs="x", outputs="x", mode="train"),
        Sharpness(level=level, inputs="x", outputs="x", mode="train"),
        Contrast(level=level, inputs="x", outputs="x", mode="train"),
        Color(level=level, inputs="x", outputs="x", mode="train"),
        Brightness(level=level, inputs="x", outputs="x", mode="train"),
        ShearX(level=level, inputs="x", outputs="x", mode="train"),
        ShearY(level=level, inputs="x", outputs="x", mode="train"),
        TranslateX(level=level, inputs="x", outputs="x", mode="train"),
        TranslateY(level=level, inputs="x", outputs="x", mode="train")
    ]
    aug_ops = [Sometimes(op, prob=get_probability(len(aug_options), 0.99)) for op in aug_options]
    pipeline = fe.Pipeline(
        train_data=train_data,
        eval_data=eval_data,
        batch_size=batch_size,
        ops=[
            PadIfNeeded(min_height=40, min_width=40, image_in="x", image_out="x", mode="train"),
            RandomCrop(32, 32, image_in="x", image_out="x", mode="train"),
            Sometimes(HorizontalFlip(image_in="x", image_out="x", mode="train"))
        ] + aug_ops + [
            Normalize(inputs="x", outputs="x", mean=(0.4914, 0.4822, 0.4465), std=(0.2471, 0.2435, 0.2616)),
            CoarseDropout(inputs="x", outputs="x", mode="train", max_holes=1),
            ChannelTranspose(inputs="x", outputs="x")
        ])

    # step 2: prepare network
    model = fe.build(model_fn=lambda: WideResNet(depth=28, num_classes=10, widen_factor=10),
                     optimizer_fn=lambda x: torch.optim.SGD(x, lr=0.1, momentum=0.9, weight_decay=0.0005))

    network = fe.Network(ops=[
        ModelOp(model=model, inputs="x", outputs="y_pred"),
        CrossEntropy(inputs=("y_pred", "y"), outputs="ce", from_logits=True),
        UpdateOp(model=model, loss_name="ce")
    ])

    # step 3 prepare estimator
    traces = [
        Accuracy(true_key="y", pred_key="y_pred"),
        LRScheduler(model=model, lr_fn=lambda epoch: cosine_decay(epoch, cycle_length=epochs, init_lr=0.1)),
        BestModelSaver(model=model, save_dir=save_dir, metric="accuracy", save_best_mode="max")
    ]
    estimator = fe.Estimator(pipeline=pipeline, network=network, epochs=epochs, traces=traces)
    return estimator


def evaluate_result(level, epochs=200):
    est = get_estimator(level=level, epochs=epochs)
    hist = est.fit(summary="exp", warmup=False)
    best_acc = float(max(hist.history["eval"]["max_accuracy"].values()))
    print("Evaluated level {}, results:{}".format(level, best_acc))
    return best_acc


def gss(a, b, total_trial=10):
    results = {}
    h = b - a
    invphi = (math.sqrt(5) - 1) / 2
    invphi2 = (3 - math.sqrt(5)) / 2
    c = int(a + invphi2 * h)
    d = int(a + invphi * h)
    yc = evaluate_result(level=c)
    results[c] = yc
    yd = evaluate_result(level=d)
    results[d] = yd
    for i in range(total_trial - 2):
        if yc > yd:
            b = d
            d = c
            yd = yc
            h = invphi * h
            c = int(a + invphi2 * h)
            if c in results:
                yc = results[c]
            else:
                yc = evaluate_result(level=c)
                results[c] = yc
        else:
            a = c
            c = d
            yc = yd
            h = invphi * h
            d = int(a + invphi * h)
            if d in results:
                yd = results[d]
            else:
                yd = evaluate_result(level=d)
                results[d] = yd
    max_value_keys = [key for key in results.keys() if results[key] == max(results.values())]
    return max_value_keys[0], results[max_value_keys[0]]


def fastestimator_run():
    best_level, best_acc = gss(a=1, b=30, total_trial=7)
    print("best level is {}, best accuracy is {}".format(best_level, best_acc))
