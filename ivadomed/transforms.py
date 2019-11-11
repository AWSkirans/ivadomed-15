import random
import numpy as np
from PIL import Image
import torchvision.transforms.functional as F
from scipy.ndimage.measurements import label, center_of_mass
from scipy.ndimage.morphology import binary_dilation, binary_fill_holes, binary_closing, generate_binary_structure

from medicaltorch import transforms as mt_transforms


def get_transform_names():
    return ['DilateGT', 'ROICrop2D']


class ROICrop2D(mt_transforms.CenterCrop2D):
    """Make a crop of a specified size around a ROI.
    :param segmentation: if it is a segmentation task.
                         When this is True (default), the crop
                         will also be applied to the ground truth.
    """
    def __init__(self, size, labeled=True):
        super().__init__(size, labeled)

    def __call__(self, sample):
        rdict = {}
        input_data = sample['input']
        roi_data = sample['roi']

        w, h = input_data.size
        th, tw = self.size
        th_half, tw_half = int(round(th / 2.)), int(round(tw / 2.))

        # compute center of mass of the ROI
        x_roi, y_roi = center_of_mass(np.array(roi_data).astype(np.int))
        x_roi, y_roi = int(round(x_roi)), int(round(y_roi))

        # compute top left corner of the crop area
        fh = y_roi - th_half
        fw = x_roi - tw_half

        params = (fh, fw, w, h)
        self.propagate_params(sample, params)

        # crop data
        input_data = F.crop(input_data, fh, fw, th, tw)
        rdict['input'] = input_data

        if self.labeled:
            gt_data = sample['gt']
            gt_metadata = sample['gt_metadata']
            gt_data = F.crop(input_data, fh, fw, th, tw)
            gt_metadata["__centercrop"] = (fh, fw, w, h)
            rdict['gt'] = gt_data

        sample.update(rdict)
        return sample

    def undo_transform(self, sample):  # not implemented yet, todo
        pass


class DilateGT(mt_transforms.MTTransform):
    """Randomly dilate a tensor ground-truth.
    :param dilation_factor: float, controls the number of dilation iterations.
                            For each individual lesion, the number of dilation iterations is computed as follows:
                                nb_it = int(round(dilation_factor * sqrt(lesion_area)))
                            If dilation_factor <= 0, then no dilation will be perfomed.
    """

    def __init__(self, dilation_factor):
        self.dil_factor = dilation_factor


    def dilate_lesion(self, arr_bin, arr_soft, label_values):
        for lb in label_values:
            # binary dilation with 1 iteration
            arr_dilated = binary_dilation(arr_bin, iterations=1)

            # isolate new voxels, i.e. the ones from the dilation
            new_voxels = np.logical_xor(arr_dilated, arr_bin).astype(np.int)

            # assign a soft value (]0, 1[) to the new voxels
            soft_new_voxels = lb * new_voxels

            # add the new voxels to the input mask
            arr_soft += soft_new_voxels
            arr_bin = (arr_soft > 0).astype(np.int)

        return arr_bin, arr_soft


    def dilate_arr(self, arr, dil_factor):
        # identify each object
        arr_labeled, lb_nb = label(arr.astype(np.int))

        # loop across each object
        arr_bin_lst, arr_soft_lst = [], []
        for obj_idx in range(1, lb_nb+1):
            arr_bin_obj = (arr_labeled == obj_idx).astype(np.int)
            arr_soft_obj = np.copy(arr_bin_obj).astype(np.float)
            # compute the number of dilation iterations depending on the size of the lesion
            nb_it = int(round(dil_factor * math.sqrt(arr_bin_obj.sum())))
            # values of the voxels added to the input mask
            soft_label_values = [x / (nb_it+1) for x in range(nb_it, 0, -1)]
            # dilate lesion
            arr_bin_dil, arr_soft_dil = self.dilate_lesion(arr_bin_obj, arr_soft_obj, soft_label_values)
            arr_bin_lst.append(arr_bin_dil)
            arr_soft_lst.append(arr_soft_dil)

        # sum dilated objects
        arr_bin_idx = np.sum(np.array(arr_bin_lst), axis=0)
        arr_soft_idx = np.sum(np.array(arr_soft_lst), axis=0)
        # clip values in case dilated voxels overlap
        arr_bin_clip, arr_soft_clip = np.clip(arr_bin_idx, 0, 1), np.clip(arr_soft_idx, 0.0, 1.0)

        return arr_soft_clip.astype(np.float), arr_bin_clip.astype(np.int)


    def random_holes(self, arr_in, arr_soft, arr_bin):
        arr_soft_out = np.copy(arr_soft)

        # coordinates of the new voxels, i.e. the ones from the dilation
        new_voxels_xx, new_voxels_yy = np.where(np.logical_xor(arr_bin, arr_in))
        nb_new_voxels = new_voxels_xx.shape[0]

        # ratio of voxels added to the input mask from the dilated mask
        new_voxel_ratio = random.random()
        # randomly select new voxel indexes to remove
        idx_to_remove = random.sample(range(nb_new_voxels),
                                            int(round(nb_new_voxels * (1 - new_voxel_ratio))))

        # set to zero the here-above randomly selected new voxels
        arr_soft_out[new_voxels_xx[idx_to_remove], new_voxels_yy[idx_to_remove]] = 0.0
        arr_bin_out = (arr_soft_out > 0).astype(np.int)

        return arr_soft_out, arr_bin_out


    def post_processing(self, arr_in, arr_soft, arr_bin, arr_dil):
        # remove new object that are not connected to the input mask
        arr_labeled, lb_nb = label(arr_bin)
        connected_to_in = arr_labeled * arr_in
        for lb in range(1, lb_nb+1):
            if np.sum(connected_to_in == lb) == 0:
                arr_soft[arr_labeled == lb] = 0

        # binary closing
        arr_bin_closed = binary_closing((arr_soft > 0).astype(bool))
        # fill binary holes
        arr_bin_filled = binary_fill_holes(arr_bin_closed)

        # recover the soft-value assigned to the filled-holes
        arr_soft_out = arr_bin_filled * arr_dil

        return arr_soft_out


    def __call__(self, sample):
        gt_data = sample['gt']
        gt_data_np = np.array(gt_data)

        if self.dil_factor > 0 and np.sum(gt_data):
            # dilation
            gt_dil, gt_dil_bin = self.dilate_arr(gt_data_np, self.dil_factor)

            # random holes in dilated area
            gt_holes, gt_holes_bin = self.random_holes(gt_data_np, gt_dil, gt_dil_bin)

            # post-processing
            gt_pp = self.post_processing(gt_data_np, gt_holes, gt_holes_bin, gt_dil)
            gt_out = gt_data_np.astype(np.float64)

            gt_t = Image.fromarray(gt_pp)
            rdict = {
                'gt': gt_t,
            }
            sample.update(rdict)

        return sample

