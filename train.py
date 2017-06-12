"""
    PyTorch training code for DiracNets

    https://github.com/szagoruyko/diracnets
    https://arxiv.org/abs/1706.00388

    2017 Sergey Zagoruyko
"""

import argparse
import os
import json
import numpy as np
import cv2
from tqdm import tqdm
import torch
import torch.optim
import torch.utils.data
import cvtransforms as T
import torchvision.datasets as datasets
from torch.autograd import Variable
import torch.nn.functional as F
import torchnet as tnt
from torchnet.engine import Engine
from diracnet import cast, data_parallel, define_diracnet
import torch.backends.cudnn as cudnn

cudnn.benchmark = True

parser = argparse.ArgumentParser(description='Wide Residual Networks')
# Model options
parser.add_argument('--model', default='resnet', type=str)
parser.add_argument('--depth', default=16, type=int)
parser.add_argument('--width', default=1, type=float)
parser.add_argument('--dataset', default='CIFAR10', type=str)
parser.add_argument('--dataroot', default='.', type=str)
parser.add_argument('--dtype', default='float', type=str)
parser.add_argument('--nthread', default=4, type=int)

# Training options
parser.add_argument('--batchSize', default=128, type=int)
parser.add_argument('--lr', default=0.1, type=float)
parser.add_argument('--epochs', default=200, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--weightDecay', default=0.0005, type=float)
parser.add_argument('--epoch_step', default='[60,120,160]', type=str,
                    help='json list with epochs to drop lr on')
parser.add_argument('--lr_decay_ratio', default=0.2, type=float)
parser.add_argument('--resume', default='', type=str)
parser.add_argument('--randomcrop_pad', default=4, type=float)

# Device options
parser.add_argument('--cuda', action='store_true')
parser.add_argument('--save', default='', type=str,
                    help='save parameters and logs in this folder')
parser.add_argument('--ngpu', default=1, type=int,
                    help='number of GPUs to use for training')
parser.add_argument('--gpu_id', default='0', type=str,
                    help='id(s) for CUDA_VISIBLE_DEVICES')


def create_iterator(opt, mode):
    if opt.dataset.startswith('CIFAR'):
        convert = tnt.transform.compose([
            lambda x: x.astype(np.float32),
            T.Normalize([125.3, 123.0, 113.9], [63.0, 62.1, 66.7]),
            lambda x: x.transpose(2,0,1),
            torch.from_numpy,
        ])

        train_transform = tnt.transform.compose([
            T.RandomHorizontalFlip(),
            T.Pad(opt.randomcrop_pad, cv2.BORDER_REFLECT),
            T.RandomCrop(32),
            convert,
        ])

        ds = getattr(datasets, opt.dataset)(opt.dataroot, train=mode, download=True)
        smode = 'train' if mode else 'test'
        ds = tnt.dataset.TensorDataset([getattr(ds, smode + '_data'),
                                        getattr(ds, smode + '_labels')])
        ds = ds.transform({0: train_transform if mode else convert})
        return ds.parallel(batch_size=opt.batchSize, shuffle=mode,
                           num_workers=opt.nthread, pin_memory=True)

    elif opt.dataset == 'ImageNet':

        def cvload(path):
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            return img

        convert = tnt.transform.compose([
            lambda x: x.astype(np.float32) / 255.0,
            T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            lambda x: x.transpose(2, 0, 1).astype(np.float32),
            torch.from_numpy,
        ])

        print("| setting up data loader...")
        if mode:
            traindir = os.path.join(opt.dataroot, 'train')
            ds = datasets.ImageFolder(traindir, tnt.transform.compose([
                T.RandomSizedCrop(224),
                T.RandomHorizontalFlip(),
                convert,
            ]), loader=cvload)
        else:
            valdir = os.path.join(opt.dataroot, 'val')
            ds = datasets.ImageFolder(valdir, tnt.transform.compose([
                T.Scale(256),
                T.CenterCrop(224),
                convert,
            ]), loader=cvload)

        return torch.utils.data.DataLoader(ds,
                                           batch_size=opt.batchSize, shuffle=mode,
                                           num_workers=opt.nthread, pin_memory=False)
    else:
        raise ValueError('dataset not understood')


def main():
    opt = parser.parse_args()
    print 'parsed options:', vars(opt)
    epoch_step = json.loads(opt.epoch_step)
    num_classes = 10 if opt.dataset == 'CIFAR10' else 100

    os.environ['CUDA_VISIBLE_DEVICES'] = opt.gpu_id
    # to prevent opencv from initializing CUDA in workers
    torch.randn(8).cuda()
    os.environ['CUDA_VISIBLE_DEVICES'] = ''

    train_loader = create_iterator(opt, True)
    test_loader = create_iterator(opt, False)

    f, params, stats = define_diracnet(opt.depth, opt.width, opt.dataset)

    def create_optimizer(opt, lr):
        print 'creating optimizer with lr = ', lr
        return torch.optim.SGD(params.values(), lr, 0.9, weight_decay=opt.weightDecay)

    optimizer = create_optimizer(opt, opt.lr)

    epoch = 0
    if opt.resume != '':
        state_dict = torch.load(opt.resume)
        epoch = state_dict['epoch']
        params_tensors, stats = state_dict['params'], state_dict['stats']
        for k, v in params.iteritems():
            v.data.copy_(params_tensors[k])
        optimizer.load_state_dict(state_dict['optimizer'])

    print '\nParameters:'
    kmax = max(len(key) for key in params.keys())
    for i, (key, v) in enumerate(params.items()):
        print str(i).ljust(5), key.ljust(kmax + 3), str(tuple(v.size())).ljust(23), torch.typename(v.data)
    print '\nAdditional buffers:'
    kmax = max(len(key) for key in stats.keys())
    for i, (key, v) in enumerate(stats.items()):
        print str(i).ljust(5), key.ljust(kmax + 3), str(tuple(v.size())).ljust(23), torch.typename(v)

    n_parameters = sum(p.numel() for p in params.values())
    print '\nTotal number of parameters:', n_parameters

    meter_loss = tnt.meter.AverageValueMeter()
    classacc = tnt.meter.ClassErrorMeter(topk=[1, 5], accuracy=True)
    timer_train = tnt.meter.TimeMeter('s')
    timer_test = tnt.meter.TimeMeter('s')

    if not os.path.exists(opt.save):
        os.mkdir(opt.save)

    def h(sample):
        inputs = Variable(cast(sample[0], opt.dtype))
        targets = Variable(cast(sample[1], 'long'))
        y = data_parallel(f, inputs, params, stats, sample[2], np.arange(opt.ngpu))
        return F.cross_entropy(y, targets), y

    def log(t, state):
        torch.save(dict(params={k: v.data for k, v in params.iteritems()},
                        stats=stats,
                        optimizer=state['optimizer'].state_dict(),
                        epoch=t['epoch']),
                   open(os.path.join(opt.save, 'model.pt7'), 'w'))
        z = vars(opt).copy(); z.update(t)
        print z

    def on_sample(state):
        state['sample'].append(state['train'])

    def on_forward(state):
        classacc.add(state['output'].data, torch.LongTensor(state['sample'][1]))
        meter_loss.add(state['loss'].data[0])

    def on_start(state):
        state['epoch'] = epoch

    def on_start_epoch(state):
        classacc.reset()
        meter_loss.reset()
        timer_train.reset()
        state['iterator'] = tqdm(train_loader, ncols=100)

        epoch = state['epoch'] + 1
        if epoch in epoch_step:
            lr = state['optimizer'].param_groups[0]['lr']
            state['optimizer'] = create_optimizer(opt, lr * opt.lr_decay_ratio)

    def on_end_epoch(state):
        train_loss = meter_loss.value()
        train_acc = classacc.value()
        train_time = timer_train.value()
        meter_loss.reset()
        classacc.reset()
        timer_test.reset()

        engine.test(h, test_loader)

        test_acc = classacc.value()
        print log({
            "train_loss": train_loss[0],
            "train_acc": train_acc,
            "test_loss": meter_loss.value()[0],
            "test_acc": test_acc,
            "epoch": state['epoch'],
            "num_classes": num_classes,
            "n_parameters": n_parameters,
            "train_time": train_time,
            "test_time": timer_test.value(),
        }, state)
        print '==> id: %s (%d/%d), test_acc: \33[91m%.2f\033[0m' % \
                (opt.save, state['epoch'], opt.epochs, test_acc[0])

    engine = Engine()
    engine.hooks['on_sample'] = on_sample
    engine.hooks['on_forward'] = on_forward
    engine.hooks['on_start_epoch'] = on_start_epoch
    engine.hooks['on_end_epoch'] = on_end_epoch
    engine.hooks['on_start'] = on_start
    engine.train(h, train_loader, opt.epochs, optimizer)


if __name__ == '__main__':
    main()
