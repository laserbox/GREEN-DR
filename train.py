import time
import os
import math
import argparse
from glob import glob
from collections import OrderedDict
import random
import warnings
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm
import pandas as pd
import joblib
import pickle

from sklearn.model_selection import StratifiedKFold, train_test_split
from skimage.io import imread

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader
from torch.utils.data.sampler import WeightedRandomSampler
import torch.backends.cudnn as cudnn
import torchvision
from torchvision import datasets, models, transforms

from lib.dataset import Dataset
from lib.models.model_factory import *
from lib.utils import *
from lib.metrics import *
from lib.losses import *
from lib.optimizers import *
from lib.preprocess import preprocess

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument('--name', default=None,
                        help='model name: (default: arch+timestamp)')
    parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet34',
                        help='model architecture: ' +
                        ' (default: resnet34)')
    parser.add_argument('--freeze_bn', default=True, type=str2bool)
    parser.add_argument('--dropout_p', default=0, type=float)
    parser.add_argument('--loss', default='CrossEntropyLoss',
                        choices=['CrossEntropyLoss', 'FocalLoss', 'MSELoss', 'multitask'])
    parser.add_argument('--reg_coef', default=1.0, type=float)
    parser.add_argument('--cls_coef', default=0.1, type=float)
    parser.add_argument('--epochs', default=30, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-b', '--batch_size', default=32, type=int,
                        metavar='N', help='mini-batch size (default: 32)')
    parser.add_argument('--img_size', default=288, type=int,
                        help='input image size (default: 288)')
    parser.add_argument('--input_size', default=256, type=int,
                        help='input image size (default: 256)')
    parser.add_argument('--optimizer', default='SGD')
    parser.add_argument('--pred_type', default='classification',
                        choices=['classification', 'regression', 'multitask'])
    parser.add_argument('--scheduler', default='CosineAnnealingLR',
                        choices=['CosineAnnealingLR', 'ReduceLROnPlateau'])
    parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float,
                        metavar='LR', help='initial learning rate')
    parser.add_argument('--min_lr', default=1e-5, type=float,
                        help='minimum learning rate')
    parser.add_argument('--T_max', default='30',type=int)         
    parser.add_argument('--factor', default=0.5, type=float)
    parser.add_argument('--patience', default=5, type=int)
    parser.add_argument('--momentum', default=0.9, type=float,
                        help='momentum')
    parser.add_argument('--weight_decay', default=1e-4, type=float,
                        help='weight decay')
    parser.add_argument('--nesterov', default=False, type=str2bool,
                        help='nesterov')
    parser.add_argument('--gpus', default='0', type=str)

    # preprocessing
    parser.add_argument('--scale_radius', default=True, type=str2bool)
    parser.add_argument('--normalize', default=False, type=str2bool)
    parser.add_argument('--padding', default=False, type=str2bool)
    parser.add_argument('--remove', default=False, type=str2bool)

    # data augmentation
    parser.add_argument('--rotate', default=True, type=str2bool)
    parser.add_argument('--rotate_min', default=-180, type=int)
    parser.add_argument('--rotate_max', default=180, type=int)
    parser.add_argument('--rescale', default=True, type=str2bool)
    parser.add_argument('--rescale_min', default=0.8889, type=float)
    parser.add_argument('--rescale_max', default=1.0, type=float)
    parser.add_argument('--shear', default=True, type=str2bool)
    parser.add_argument('--shear_min', default=-36, type=int)
    parser.add_argument('--shear_max', default=36, type=int)
    parser.add_argument('--translate', default=False, type=str2bool)
    parser.add_argument('--translate_min', default=0, type=float)
    parser.add_argument('--translate_max', default=0, type=float)
    parser.add_argument('--flip', default=True, type=str2bool)
    parser.add_argument('--contrast', default=True, type=str2bool)
    parser.add_argument('--contrast_min', default=0.9, type=float)
    parser.add_argument('--contrast_max', default=1.1, type=float)
    parser.add_argument('--random_erase', default=False, type=str2bool)
    parser.add_argument('--random_erase_prob', default=0.5, type=float)
    parser.add_argument('--random_erase_sl', default=0.02, type=float)
    parser.add_argument('--random_erase_sh', default=0.4, type=float)
    parser.add_argument('--random_erase_r', default=0.3, type=float)

    # dataset
    parser.add_argument('--train_dataset',
                        default='aptos2019, diabetic_retinopathy')
    parser.add_argument('--cv', default=True, type=str2bool)
    parser.add_argument('--n_splits', default=5, type=int)
    parser.add_argument('--remove_duplicate', default=False, type=str2bool)
    parser.add_argument('--class_aware', default=False, type=str2bool)

    # pseudo label
    parser.add_argument('--pretrained_model')
    parser.add_argument('--pseudo_labels')

    args = parser.parse_args()

    return args


def train(args, train_loader, model, criterion, optimizer, epoch):
    losses = AverageMeter()
    scores = AverageMeter()
    ac_scores = AverageMeter()
    f1_scores = AverageMeter()

    model.train()

    for i, (input, target) in tqdm(enumerate(train_loader), total=len(train_loader)):
        input = input.cuda()
        target = target.cuda()

        output, adj_weight, x = model(input)
        if args.pred_type == 'classification':
            loss = criterion(output, target)
        elif args.pred_type == 'regression':
            loss = criterion(output.view(-1), target.float())
        elif args.pred_type == 'multitask':
            loss = args.reg_coef * criterion['regression'](output[:, 0], target.float()) + \
                   args.cls_coef * criterion['classification'](output[:, 1:], target)
            output = output[:, 0].unsqueeze(1)

        # compute gradient and do optimizing step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        score = quadratic_weighted_kappa(output, target)
        # output = F.softmax(output, dim=1)
        # score = kappa(output, target)
        ac_score = compute_accuracy(output, target)
        f1_score = compute_f1(output, target)


        losses.update(loss.item(), input.size(0))
        scores.update(score, input.size(0))
        ac_scores.update(ac_score, input.size(0))
        f1_scores.update(f1_score, input.size(0))
    
    print(adj_weight)
    # print(x[0:10])
    return losses.avg, scores.avg, ac_scores.avg, f1_scores.avg

def validate(args, val_loader, model, criterion):
    losses = AverageMeter()
    scores = AverageMeter()
    ac_scores = AverageMeter()
    f1_scores = AverageMeter()

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        for i, (input, target) in tqdm(enumerate(val_loader), total=len(val_loader)):
            input = input.cuda()
            target = target.cuda()

            output, adj, _ = model(input)

            if args.pred_type == 'classification':
                loss = criterion(output, target)
            elif args.pred_type == 'regression':
                loss = criterion(output.view(-1), target.float())
            elif args.pred_type == 'multitask':
                loss = args.reg_coef * criterion['regression'](output[:, 0], target.float()) + \
                       args.cls_coef * criterion['classification'](output[:, 1:], target)
                output = output[:, 0].unsqueeze(1)
            # print(loss)
            score = quadratic_weighted_kappa(output, target)
            # output = F.softmax(output, dim=1)
            # score = kappa(output, target)
            ac_score = compute_accuracy(output, target)
            f1_score = compute_f1(output, target)

            losses.update(loss.item(), input.size(0))
            scores.update(score, input.size(0))
            ac_scores.update(ac_score, input.size(0))
            f1_scores.update(f1_score, input.size(0))

    return losses.avg, scores.avg, ac_scores.avg, f1_scores.avg, adj

def main():
    args = parse_args()

    if args.name is None:
        args.name = '%s_%s_%s' % ('gcn', args.arch, args.train_dataset)

    if not os.path.exists('models/%s' % args.name):
        os.makedirs('models/%s' % args.name)

    print('Config -----')
    for arg in vars(args):
        print('- %s: %s' % (arg, getattr(args, arg)))
    print('------------')

    with open('models/%s/args.txt' % args.name, 'w') as f:
        for arg in vars(args):
            print('- %s: %s' % (arg, getattr(args, arg)), file=f)

    joblib.dump(args, 'models/%s/args.pkl' % args.name)

    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus

    if args.loss == 'CrossEntropyLoss':
        criterion = nn.CrossEntropyLoss().cuda()
    elif args.loss == 'FocalLoss':
        criterion = FocalLoss().cuda()
    elif args.loss == 'MSELoss':
        criterion = nn.MSELoss().cuda()
    elif args.loss == 'multitask':
        criterion = {
            'classification': nn.CrossEntropyLoss().cuda(),
            'regression': nn.MSELoss().cuda(),
        }
    else:
        raise NotImplementedError

    if args.pred_type == 'classification':
        num_outputs = 5
    elif args.pred_type == 'regression':
        num_outputs = 1
    elif args.loss == 'multitask':
        num_outputs = 6
    else:
        raise NotImplementedError

    cudnn.benchmark = True

    train_transform = []
    train_transform = transforms.Compose([
        transforms.Resize((args.img_size, args.img_size)),
        transforms.RandomAffine(
            degrees=(args.rotate_min, args.rotate_max) if args.rotate else 0,
            translate=(args.translate_min, args.translate_max) if args.translate else None,
            scale=(args.rescale_min, args.rescale_max) if args.rescale else None,
            shear=(args.shear_min, args.shear_max) if args.shear else None,
        ),
        transforms.CenterCrop(args.input_size),
        transforms.RandomHorizontalFlip(p=0.5 if args.flip else 0),
        transforms.RandomVerticalFlip(p=0.5 if args.flip else 0),
        transforms.ColorJitter(
            brightness=0,
            contrast=args.contrast,
            saturation=0,
            hue=0),
        RandomErase(
            prob=args.random_erase_prob if args.random_erase else 0,
            sl=args.random_erase_sl,
            sh=args.random_erase_sh,
            r=args.random_erase_r),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])

    val_transform = transforms.Compose([
        # transforms.Resize((args.img_size, args.input_size)),
        transforms.Resize((args.input_size, args.input_size)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])

    # data loading code
    if 'diabetic_retinopathy' in args.train_dataset:
        diabetic_retinopathy_dir = preprocess(
            'diabetic_retinopathy',
            args.img_size,
            scale=args.scale_radius,
            norm=args.normalize,
            pad=args.padding,
            remove=args.remove)
        diabetic_retinopathy_df = pd.read_csv('inputs/diabetic-retinopathy-resized/trainLabels.csv')
        diabetic_retinopathy_img_paths = \
            diabetic_retinopathy_dir + '/' + diabetic_retinopathy_df['image'].values + '.jpeg'
        diabetic_retinopathy_labels = diabetic_retinopathy_df['level'].values

    if 'aptos2019' in args.train_dataset:
        aptos2019_dir = preprocess(
            'aptos2019',
            args.img_size,
            scale=args.scale_radius,
            norm=args.normalize,
            pad=args.padding,
            remove=args.remove)
        aptos2019_df = pd.read_csv('inputs/train.csv')
        aptos2019_img_paths = aptos2019_dir + '/' + aptos2019_df['id_code'].values + '.png'
        aptos2019_labels = aptos2019_df['diagnosis'].values
    # take parts of aptos2019 as train_set, take the last as val_set.
    if args.train_dataset == 'aptos2019':
        skf = StratifiedKFold(n_splits=args.n_splits, shuffle=True, random_state=41)
        img_paths = []
        labels = []
        for fold, (train_idx, val_idx) in enumerate(skf.split(aptos2019_img_paths, aptos2019_labels)):
            img_paths.append((aptos2019_img_paths[train_idx], aptos2019_img_paths[val_idx]))
            labels.append((aptos2019_labels[train_idx], aptos2019_labels[val_idx]))
    # take parts of diabetic_retinopathy as train_set, take the last as val_set.    
    elif args.train_dataset == 'diabetic_retinopathy':
        skf = StratifiedKFold(n_splits=args.n_splits, shuffle=True, random_state=41)
        img_paths = []
        labels = []
        for fold, (train_idx, val_idx) in enumerate(skf.split(diabetic_retinopathy_img_paths, diabetic_retinopathy_labels)):
            img_paths.append((diabetic_retinopathy_img_paths[train_idx], diabetic_retinopathy_img_paths[val_idx]))
            labels.append((diabetic_retinopathy_labels[train_idx], diabetic_retinopathy_labels[val_idx]))
    # take parts of aptos2019 and all diabetic_retinopathy as train_set, take the last aptos2019 as val_set.
    elif 'diabetic_retinopathy' in args.train_dataset and 'aptos2019' in args.train_dataset:
        skf = StratifiedKFold(n_splits=args.n_splits, shuffle=True, random_state=41)
        img_paths = []
        labels = []
        for fold, (train_idx, val_idx) in enumerate(skf.split(aptos2019_img_paths, aptos2019_labels)):
            img_paths.append((np.hstack((aptos2019_img_paths[train_idx], diabetic_retinopathy_img_paths)), aptos2019_img_paths[val_idx]))
            labels.append((np.hstack((aptos2019_labels[train_idx], diabetic_retinopathy_labels)), aptos2019_labels[val_idx]))
    # else:
    #     raise NotImplementedError

    if args.pseudo_labels:
        test_df = pd.read_csv('probs/%s.csv' % args.pseudo_labels)
        test_dir = preprocess(
            'test',
            args.img_size,
            scale=args.scale_radius,
            norm=args.normalize,
            pad=args.padding,
            remove=args.remove)
        test_img_paths = test_dir + '/' + test_df['id_code'].values + '.png'
        test_labels = test_df['diagnosis'].values
        for fold in range(len(img_paths)):
            img_paths[fold] = (np.hstack((img_paths[fold][0], test_img_paths)), img_paths[fold][1])
            labels[fold] = (np.hstack((labels[fold][0], test_labels)), labels[fold][1])

    if 'messidor' in args.train_dataset:
        test_dir = preprocess(
            'messidor',
            args.img_size,
            scale=args.scale_radius,
            norm=args.normalize,
            pad=args.padding,
            remove=args.remove)

    folds = []
    best_losses = []
    best_scores = []
    best_ac_scores = []
    best_f1_scores = []
    best_epochs = []

    best_losses_1 = []
    best_scores_1 = []
    best_ac_scores_1 = []
    best_f1_scores_1 = []
    best_epochs_1 = []


    for fold, ((train_img_paths, val_img_paths), (train_labels, val_labels)) in enumerate(zip(img_paths, labels)):
        print('Fold [%d/%d]' %(fold+1, len(img_paths)))

        if os.path.exists('models/%s/model_%d.pth' % (args.name, fold+1)):
            log = pd.read_csv('models/%s/log_%d.csv' %(args.name, fold+1))
            best_loss, best_score, best_ac_score = log.loc[log['val_loss'].values.argmin(), ['val_loss', 'val_score', 'val_ac_score']].values
            folds.append(str(fold + 1))
            best_losses.append(best_loss)
            best_scores.append(best_score)
            best_ac_scores.append(best_ac_score)
            continue

        if args.remove_duplicate:
            md5_df = pd.read_csv('inputs/strMd5.csv')
            duplicate_img_paths = aptos2019_dir + '/' + md5_df[(md5_df.strMd5_count > 1) & (~md5_df.diagnosis.isnull())]['id_code'].values + '.png'
            print(duplicate_img_paths)
            for duplicate_img_path in duplicate_img_paths:
                train_labels = train_labels[train_img_paths != duplicate_img_path]
                train_img_paths = train_img_paths[train_img_paths != duplicate_img_path]
                val_labels = val_labels[val_img_paths != duplicate_img_path]
                val_img_paths = val_img_paths[val_img_paths != duplicate_img_path]

        # train
        train_set = Dataset(
            train_img_paths,
            train_labels,
            transform=train_transform)

        #  _, class_sample_counts = np.unique(train_labels, return_counts=True)
        # print(class_sample_counts)
        # weights = 1. / torch.tensor(class_sample_counts, dtype=torch.float)
        # weights = np.array([0.2, 0.1, 0.6, 0.1, 0.1])
        # samples_weights = weights[train_labels]
        # sampler = WeightedRandomSampler(
        #     weights=samples_weights,
        #     num_samples=11000,
        #     replacement=False)

        # _, class_count = np.unique(val_labels, return_counts=True)
        # print(class_count)

        train_loader = torch.utils.data.DataLoader(
            train_set,
            batch_size=args.batch_size,
            shuffle=False if args.class_aware else True,
            num_workers=4,
            sampler=sampler if args.class_aware else None)

        val_set = Dataset(
            val_img_paths,
            val_labels,
            transform=val_transform)
        val_loader = torch.utils.data.DataLoader(
            val_set,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=4)

        # create model

        model = get_final_model(model_name=args.arch,
                      num_outputs=num_outputs)

        device = torch.device('cuda')
        if torch.cuda.device_count() > 1:
            model = nn.DataParallel(model)
        model.to(device)
        # model = model.cuda()
        if args.pretrained_model is not None:
            model.load_state_dict(torch.load('models/%s/model_%d.pth' % (args.pretrained_model, fold+1)))

        # print(model)

        if args.optimizer == 'Adam':
            optimizer = optim.Adam(
                filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr)
        elif args.optimizer == 'AdamW':
            optimizer = optim.AdamW(
                filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr)
        elif args.optimizer == 'RAdam':
            optimizer = RAdam(
                filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr)
        elif args.optimizer == 'SGD':
            optimizer = optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr,
                                  momentum=args.momentum, weight_decay=args.weight_decay, nesterov=args.nesterov)
            # optimizer = optim.SGD(model.get_config_optim(args.lr, args.lr, args.lr), lr=args.lr,
            #                       momentum=args.momentum, weight_decay=args.weight_decay, nesterov=args.nesterov)        

        if args.scheduler == 'CosineAnnealingLR':
            scheduler = lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=args.min_lr)
        elif args.scheduler == 'ReduceLROnPlateau':
            scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, factor=args.factor, patience=args.patience,
                                                       verbose=1, min_lr=args.min_lr)

        # for name, param in model.named_parameters():
        #     if param.requires_grad:
        #         print(name)
        log = pd.DataFrame(index=[], columns=[
            'epoch', 'loss', 'score', 'ac_score', 'f1_score', 'val_loss', 'val_score', 'val_ac_score', 'val_f1_score'
        ])
        log = {
            'epoch': [],
            'loss': [],
            'score': [],
            'ac_score': [],
            'f1_score': [],
            'val_loss': [],
            'val_score': [],
            'val_ac_score': [],
            'val_f1_score': [],
        }

        best_loss = float('inf')
        best_score = 0
        best_ac_score = 0
        best_f1_score = 0
        best_epoch = 0

        best_loss_1 = float('inf')
        best_score_1 = 0
        best_ac_score_1 = 0
        best_f1_score_1 = 0
        best_epoch_1 = 0

        for epoch in range(args.epochs):
            print('Epoch [%d/%d]' % (epoch + 1, args.epochs))

            # train for one epoch
            train_loss, train_score, train_ac_score, train_f1_score = train(
                args, train_loader, model, criterion, optimizer, epoch)

            # evaluate on validation set
            val_loss, val_score, val_ac_score, val_f1_score, adj = validate(args, val_loader, model, criterion)

            print('cnn_lr %.6f - adj_lr %.6f' %(optimizer.param_groups[0]['lr'], optimizer.param_groups[-1]['lr']))

            if args.scheduler == 'CosineAnnealingLR':
                scheduler.step()
            elif args.scheduler == 'ReduceLROnPlateau':
                scheduler.step(val_loss)

            print('loss %.4f - score %.4f - ac_score %.4f - f1_score %.4f - val_loss %.4f - val_score %.4f - val_ac_score %.4f - val_f1_score %.4f'
                  % (train_loss, train_score, train_ac_score, train_f1_score, val_loss, val_score, val_ac_score, val_f1_score))

            log['epoch'].append(epoch)
            log['loss'].append(train_loss)
            log['score'].append(train_score)
            log['ac_score'].append(train_ac_score)
            log['f1_score'].append(train_f1_score)
            log['val_loss'].append(val_loss)
            log['val_score'].append(val_score)
            log['val_ac_score'].append(val_ac_score)
            log['val_f1_score'].append(val_f1_score)

            pd.DataFrame(log).to_csv('models/%s/log_%d.csv' % (args.name, fold+1), index=False)

            if val_score > best_score:
                # torch.save(model.state_dict(), 'models/%s/model_%d.pth' % (args.name, fold+1))
                pickle.dump(adj.data.cpu().numpy(), open('./models/%s/adj_%d.pkl' % (args.name, fold+1), 'wb'))
                best_loss = val_loss
                best_score = val_score
                best_ac_score = val_ac_score
                best_f1_score = val_f1_score
                best_epoch = epoch
                print("=> saved best model")
            if val_ac_score > best_ac_score_1:
                # torch.save(model.state_dict(), 'models/%s/model_1_%d.pth' % (args.name, fold+1))
                best_loss_1 = val_loss
                best_score_1 = val_score
                best_ac_score_1 = val_ac_score
                best_f1_score_1 = val_f1_score
                best_epoch_1 = epoch
                print("=> saved best model_1")

        print('val_loss:  %f' % best_loss)
        print('val_score: %f' % best_score)
        print('val_ac_score: %f' % best_ac_score)
        print('val_f1_score: %f' % best_f1_score)

        print('val_loss_1:  %f' % best_loss_1)
        print('val_score_1: %f' % best_score_1)
        print('val_ac_score_1: %f' % best_ac_score_1)
        print('val_f1_score_1: %f' % best_f1_score_1)

        folds.append(str(fold + 1))
        best_losses.append(best_loss)
        best_scores.append(best_score)
        best_ac_scores.append(best_ac_score)
        best_f1_scores.append(best_f1_score)
        best_epochs.append(best_epoch)

        best_losses_1.append(best_loss_1)
        best_scores_1.append(best_score_1)
        best_ac_scores_1.append(best_ac_score_1)
        best_f1_scores_1.append(best_f1_score_1)
        best_epochs_1.append(best_epoch_1)

        results = pd.DataFrame({
            'fold': folds + ['mean'],
            'best_loss': best_losses + [np.mean(best_losses)],
            'best_score': best_scores + [np.mean(best_scores)],
            'best_ac_score': best_ac_scores + [np.mean(best_ac_scores)],
            'best_f1_score': best_f1_scores + [np.mean(best_f1_scores)],
            'best_epoch': best_epochs + [''],
        })

        results_1 = pd.DataFrame({
            'fold': folds + ['mean'],
            'best_loss_1': best_losses_1 + [np.mean(best_losses_1)],
            'best_score_1': best_scores_1 + [np.mean(best_scores_1)],
            'best_ac_score_1': best_ac_scores_1 + [np.mean(best_ac_scores_1)],
            'best_f1_score_1': best_f1_scores_1 + [np.mean(best_f1_scores_1)],
            'best_epoch': best_epochs_1 + [''],
        })

        print(results)
        print(results_1)
        results.to_csv('models/%s/results.csv' % args.name, index=False)
        results_1.to_csv('models/%s/results_1.csv' % args.name, index=False)

        # model.load_state_dict(torch.load('models/%s/model_%d.pth' % (args.name, fold+1)))
        # model.remove_gacngate=True
        # val_loss, val_score, val_ac_score, val_f1_score = gcn_validate(args, val_loader, model, criterion)

        # remove_losses.append(val_loss)
        # remove_scores.append(val_score)
        # remove_ac_scores.append(val_ac_score)
        # remove_f1_scores.append(val_f1_score)

        # remove_gcn_result = pd.DataFrame({
        #     'fold': folds + ['mean'],
        #     'remove_loss': remove_losses + [np.mean(remove_losses)],
        #     'remove_score': remove_scores + [np.mean(remove_scores)],
        #     'remove_ac_score': remove_ac_scores + [np.mean(remove_ac_scores)],
        #     'remove_f1_score': remove_f1_scores + [np.mean(remove_f1_scores)],
        # })
        # print(remove_gcn_result)
        # remove_gcn_result.to_csv('models/%s/remove_gcn_result.csv' % args.name, index=False)

        # model.load_state_dict(torch.load('models/%s/model_1_%d.pth' % (args.name, fold+1)))
        # model.remove_gacngate=True
        # val_loss, val_score, val_ac_score, val_f1_score = gcn_validate(args, val_loader, model, criterion)

        # remove_losses_1.append(val_loss)
        # remove_scores_1.append(val_score)
        # remove_ac_scores_1.append(val_ac_score)
        # remove_f1_scores_1.append(val_f1_score)

        # remove_gcn_result_1 = pd.DataFrame({
        #     'fold': folds + ['mean'],
        #     'remove_loss_1': remove_losses_1 + [np.mean(remove_losses_1)],
        #     'remove_score_1': remove_scores_1 + [np.mean(remove_scores_1)],
        #     'remove_ac_score_1': remove_ac_scores_1 + [np.mean(remove_ac_scores_1)],
        #     'remove_f1_score_1': remove_f1_scores_1 + [np.mean(remove_f1_scores_1)],
        # })
        # print(remove_gcn_result_1)
        # remove_gcn_result.to_csv('models/%s/remove_gcn_result_1.csv' % args.name, index=False)

        torch.cuda.empty_cache()

        if not args.cv:
            break


if __name__ == '__main__':
    main()
