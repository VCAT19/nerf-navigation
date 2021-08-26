import numpy as np
import torch
import torch.nn as nn
import cv2
import skimage
import matplotlib.pyplot as plt

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

TEST = False

fine_size = 256

l = 1

N = 1

#coarse = lambda x, y: torch.linalg.norm((x - y), ord=2, dim=1)/(torch.sum(torch.linalg.norm((x - y), ord=2, dim=1)))
coarse = lambda x, y: torch.sum((x - y)**2, dim=1)/torch.sum(torch.sum((x - y)**2, dim=1))
coarse_depth = lambda x, y: (x - y)**2 / torch.sum((x - y)**2)

#Helper Functions
def find_POI(img_rgb, DEBUG=False): # img - RGB image in range 0...255
    img = np.copy(img_rgb)
    img_gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    sift = cv2.SIFT_create()
    keypoints = sift.detect(img_gray, None)

    xy = [keypoint.pt for keypoint in keypoints]
    xy = np.array(xy).astype(int)
    # Remove duplicate points
    xy_set = set(tuple(point) for point in xy)
    xy = np.array([list(point) for point in xy_set]).astype(int)
    return xy # pixel coordinates

img2mse = lambda x, y : torch.mean((x - y) ** 2)
#depth2mse = lambda x, y, z : torch.mean(((x - y) ** 2) * 1./torch.sqrt((torch.max(1e-10 * torch.ones_like(z), z))))
depth2mse = lambda x, y, z : torch.mean(((x - y) ** 2))

rot_psi = lambda phi: np.array([
        [1, 0, 0, 0],
        [0, np.cos(phi), -np.sin(phi), 0],
        [0, np.sin(phi), np.cos(phi), 0],
        [0, 0, 0, 1]])

rot_theta = lambda th: np.array([
        [np.cos(th), 0, -np.sin(th), 0],
        [0, 1, 0, 0],
        [np.sin(th), 0, np.cos(th), 0],
        [0, 0, 0, 1]])

rot_phi = lambda psi: np.array([
        [np.cos(psi), -np.sin(psi), 0, 0],
        [np.sin(psi), np.cos(psi), 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1]])

trans_t = lambda t: np.array([
        [1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, t],
        [0, 0, 0, 1]])

def vec2ss_matrix(vector):  # vector to skewsym. matrix

    ss_matrix = torch.zeros((3,3))
    ss_matrix[0, 1] = -vector[2]
    ss_matrix[0, 2] = vector[1]
    ss_matrix[1, 0] = vector[2]
    ss_matrix[1, 2] = -vector[0]
    ss_matrix[2, 0] = -vector[1]
    ss_matrix[2, 1] = vector[0]

    return ss_matrix


class camera_transf(nn.Module):
    def __init__(self):
        super(camera_transf, self).__init__()
        self.w = nn.Parameter(torch.normal(0., 1e-6, size=(3,)))
        self.v = nn.Parameter(torch.normal(0., 1e-6, size=(3,)))
        self.theta = nn.Parameter(torch.normal(0., 1e-6, size=()))

    def forward(self, x):
        exp_i = torch.zeros((4,4))
        w_skewsym = vec2ss_matrix(self.w)
        v_skewsym = vec2ss_matrix(self.v)
        exp_i[:3, :3] = torch.eye(3) + torch.sin(self.theta) * w_skewsym + (1 - torch.cos(self.theta)) * torch.matmul(w_skewsym, w_skewsym)
        exp_i[:3, 3] = torch.matmul(torch.eye(3) * self.theta + (1 - torch.cos(self.theta)) * w_skewsym + (self.theta - torch.sin(self.theta)) * torch.matmul(w_skewsym, w_skewsym), self.v)
        exp_i[3, 3] = 1.
        T_i = torch.matmul(exp_i, x)
        return T_i

class Estimator():
    def __init__(self, N_iter, batch_size, sampling_strategy, renderer, dil_iter=3, kernel_size=5, lrate=.01, noise=None, sigma=0.01, amount=0.8, delta_brightness=0.) -> None:
    # Parameters
        self.batch_size = batch_size
        self.kernel_size = kernel_size
        self.dil_iter = dil_iter

        self.lrate = lrate
        self.sampling_strategy = sampling_strategy
        #delta_phi, delta_theta, delta_psi, delta_t = args.delta_phi, args.delta_theta, args.delta_psi, args.delta_t
        self.noise, self.sigma, self.amount = noise, sigma, amount
        self.delta_brightness = delta_brightness

        self.renderer = renderer

        self.iter = N_iter

        # create meshgrid from the observed image
        self.W, self.H, self.focal = self.renderer.hwf
        self.coords = np.asarray(np.stack(np.meshgrid(np.linspace(0, self.W - 1, self.W), np.linspace(0, self.H - 1, self.H)), -1),
                            dtype=int)

    def estimate_pose(self, start_pose, obs_img, obs_img_pose, obs_img_depth=None):

        obs_img = (np.array(obs_img) / 255.).astype(np.float32)

        # change brightness of the observed image
        if self.delta_brightness != 0:
            obs_img = (np.array(obs_img) / 255.).astype(np.float32)
            obs_img = cv2.cvtColor(obs_img, cv2.COLOR_RGB2HSV)
            if self.delta_brightness < 0:
                obs_img[..., 2][obs_img[..., 2] < abs(self.delta_brightness)] = 0.
                obs_img[..., 2][obs_img[..., 2] >= abs(self.delta_brightness)] += self.delta_brightness
            else:
                lim = 1. - self.delta_brightness
                obs_img[..., 2][obs_img[..., 2] > lim] = 1.
                obs_img[..., 2][obs_img[..., 2] <= lim] += self.delta_brightness
            obs_img = cv2.cvtColor(obs_img, cv2.COLOR_HSV2RGB)

        # apply noise to the observed image
        if self.noise == 'gaussian':
            obs_img_noised = skimage.util.random_noise(obs_img, mode='gaussian', var=self.sigma**2)
        elif self.noise == 's_and_p':
            obs_img_noised = skimage.util.random_noise(obs_img, mode='s&p', amount=self.amount)
        elif self.noise == 'pepper':
            obs_img_noised = skimage.util.random_noise(obs_img, mode='pepper', amount=self.amount)
        elif self.noise == 'salt':
            obs_img_noised = skimage.util.random_noise(obs_img, mode='salt', amount=self.amount)
        elif self.noise == 'poisson':
            obs_img_noised = skimage.util.random_noise(obs_img, mode='poisson')
        else:
            obs_img_noised = obs_img

        obs_img_noised_POI = (np.array(obs_img_noised) * 255).astype(np.uint8)

        if self.sampling_strategy == 'interest_regions' or self.sampling_strategy == 'interest_points':
            # find points of interest of the observed image
            POI = find_POI(obs_img_noised_POI, False)  # xy pixel coordinates of points of interest (N x 2)

        #obs_img_noised = (np.array(obs_img_noised) / 255.).astype(np.float32)

        if self.sampling_strategy == 'interest_regions':
            # create sampling mask for interest region sampling strategy
            interest_regions = np.zeros((self.H, self.W, ), dtype=np.uint8)
            interest_regions[POI[:,1], POI[:,0]] = 1
            I = self.dil_iter
            interest_regions = cv2.dilate(interest_regions, np.ones((self.kernel_size, self.kernel_size), np.uint8), iterations=I)
            interest_regions = np.array(interest_regions, dtype=bool)
            interest_regions = self.coords[interest_regions]

        # not_POI contains all points except of POI
        coords = self.coords.reshape(self.H * self.W, 2)

        if self.sampling_strategy == 'interest_points':
            not_POI = set(tuple(point) for point in coords) - set(tuple(point) for point in POI)
            not_POI = np.array([list(point) for point in not_POI]).astype(int)

        # Create pose transformation model
        start_pose = torch.Tensor(start_pose).to(device)
        cam_transf = camera_transf().to(device)
        optimizer = torch.optim.Adam(params=cam_transf.parameters(), lr=self.lrate, betas=(0.9, 0.999))

        # calculate angles and translation of the observed image's pose
        phi_ref = np.arctan2(obs_img_pose[1,0], obs_img_pose[0,0])*180/np.pi
        theta_ref = np.arctan2(-obs_img_pose[2, 0], np.sqrt(obs_img_pose[2, 1]**2 + obs_img_pose[2, 2]**2))*180/np.pi
        psi_ref = np.arctan2(obs_img_pose[2, 1], obs_img_pose[2, 2])*180/np.pi
        translation_ref = np.sqrt(obs_img_pose[0,3]**2 + obs_img_pose[1,3]**2 + obs_img_pose[2,3]**2)

        for k in range(self.iter):

            if k % N == 0:

                if self.sampling_strategy == 'random':
                    rand_inds = np.random.choice(coords.shape[0], size=self.batch_size, replace=False)
                    batch = coords[rand_inds]

                elif self.sampling_strategy == 'interest_points':
                    if POI.shape[0] >= self.batch_size:
                        rand_inds = np.random.choice(POI.shape[0], size=self.batch_size, replace=False)
                        batch = POI[rand_inds]
                    else:
                        batch = np.zeros((self.batch_size, 2), dtype=np.int)
                        batch[:POI.shape[0]] = POI
                        rand_inds = np.random.choice(not_POI.shape[0], size=self.batch_size-POI.shape[0], replace=False)
                        batch[POI.shape[0]:] = not_POI[rand_inds]

                elif self.sampling_strategy == 'interest_regions':
                    rand_inds = np.random.choice(interest_regions.shape[0], size=self.batch_size, replace=False)
                    batch = interest_regions[rand_inds]

                else:
                    print('Unknown sampling strategy')
                    return

            target_s = obs_img_noised[batch[:, 1], batch[:, 0]]
            target_s = torch.Tensor(target_s).to(device)

            depth_s = obs_img_depth[batch[:, 1], batch[:, 0]]
            depth_s = torch.Tensor(depth_s).to(device)

            pose = cam_transf(start_pose)

            rgb, depth = self.renderer.get_img_from_pix(batch, pose, HW=False, NeedDepth=True)

            depth_val = depth[0]
            depth_var = depth[1]

            optimizer.zero_grad()

            #Performs another layer of interest region sampling
            if TEST == True:

                #Distribution of Coarse Loss
                #coarse_dist = coarse(rgb, target_s)
                coarse_dist = coarse_depth(depth_val, depth_s)
                coarse_dist = coarse_dist.cpu().detach().numpy()

                #Sample from this distribution to get the (x,y) coordinates (and their surrounding regions) that we will later sample the fine points from.
                ind_fine = np.random.choice(rgb.shape[0], size=fine_size, replace=True, p=coarse_dist)
                batch_fine = batch[ind_fine]

                points_fine = np.empty((0, 2), int)
                for inter_point in batch_fine:
                    # create sampling mask for interest region sampling strategy
                    interest_regions_fine = np.zeros((self.H, self.W, ), dtype=np.uint8)
                    interest_regions_fine[inter_point[1], inter_point[0]] = 1
                    I = self.dil_iter
                    interest_regions_fine = cv2.dilate(interest_regions_fine, np.ones((self.kernel_size, self.kernel_size), np.uint8), iterations=I)
                    interest_regions_fine = np.array(interest_regions_fine, dtype=bool)
                    interest_regions_fine = self.coords[interest_regions_fine]

                    #Sample in interest region around the fine points
                    rand_inds_fine = np.random.choice(interest_regions_fine.shape[0])
                    point_fine = interest_regions_fine[rand_inds_fine]
                    points_fine = np.append(points_fine,np.array([point_fine]), axis=0)


                #Points fine is like batch, but for all the fine points
                target_s_fine = obs_img_noised[points_fine[:, 1], points_fine[:, 0]]
                target_s_fine = torch.Tensor(target_s_fine).to(device)

                depth_s_fine = obs_img_depth[points_fine[:, 1], points_fine[:, 0]]
                depth_s_fine = torch.Tensor(depth_s_fine).to(device)

                rgb_fine, depth_fine = self.renderer.get_img_from_pix(points_fine, pose, HW=False, NeedDepth=True)

                depth_fine_val = depth_fine[0]
                depth_fine_var = depth_fine[1]

                loss_rgb_fine = img2mse(rgb_fine, target_s_fine)

                loss_depth_fine = depth2mse(depth_fine_val, depth_s_fine, depth_fine_var)

                loss_rgb = img2mse(rgb, target_s)

                loss_depth = depth2mse(depth_val, depth_s, depth_var)

                loss = loss_rgb + loss_depth + loss_rgb_fine + loss_depth_fine

            else:
                loss_rgb = img2mse(rgb, target_s)

                loss_depth = depth2mse(depth_val, depth_s, depth_var)

                loss = l*loss_rgb + loss_depth

                #loss = loss_rgb

            loss.backward()
            optimizer.step()

            new_lrate = self.lrate * (0.8 ** ((k + 1) / 100))
            for param_group in optimizer.param_groups:
                param_group['lr'] = new_lrate

            if (k + 1) % 20 == 0 or k == 0:
                print('Step: ', k)
                print('Loss: ', loss)

                with torch.no_grad():
                    pose_dummy = pose.cpu().detach().numpy()
                    # calculate angles and translation of the optimized pose
                    phi = np.arctan2(pose_dummy[1, 0], pose_dummy[0, 0]) * 180 / np.pi
                    theta = np.arctan2(-pose_dummy[2, 0], np.sqrt(pose_dummy[2, 1] ** 2 + pose_dummy[2, 2] ** 2)) * 180 / np.pi
                    psi = np.arctan2(pose_dummy[2, 1], pose_dummy[2, 2]) * 180 / np.pi
                    translation = np.sqrt(pose_dummy[0,3]**2 + pose_dummy[1,3]**2 + pose_dummy[2,3]**2)
                    #translation = pose_dummy[2, 3]
                    # calculate error between optimized and observed pose
                    phi_error = abs(phi_ref - phi) if abs(phi_ref - phi)<300 else abs(abs(phi_ref - phi)-360)
                    theta_error = abs(theta_ref - theta) if abs(theta_ref - theta)<300 else abs(abs(theta_ref - theta)-360)
                    psi_error = abs(psi_ref - psi) if abs(psi_ref - psi)<300 else abs(abs(psi_ref - psi)-360)
                    rot_error = phi_error + theta_error + psi_error
                    translation_error = abs(translation_ref - translation)
                    print('Rotation error: ', rot_error)
                    print('Translation error: ', translation_error)
                    print('-----------------------------------')

                    '''
                    if (k+1) % 100 == 0:
                        img_dummy = self.renderer.get_img_from_pose(pose)
                        plt.figure()
                        plt.imshow(img_dummy.cpu().detach().numpy())
                        plt.show()
                        plt.close()
                    '''
                    
        
        self.pose_prior = pose.cpu().detach().numpy()

        return pose.cpu().detach().numpy()