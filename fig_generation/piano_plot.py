import torch
torch.autograd.set_detect_anomaly(True)
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

import matplotlib.pyplot as plt
import matplotlib.cm as cm

from torchtyping import TensorType, patch_typeguard
from typeguard import typechecked

patch_typeguard()

@typechecked
def nerf(points: TensorType["batch":..., 2]) -> TensorType["batch":...]:
    x = points[..., 0]
    y = points[..., 1]

    sharpness = 8
    return torch.sigmoid(sharpness * (y-1 )) * torch.sigmoid(sharpness * (x-1 )) 


def plot_nerf(ax, nerf):
    linspace = torch.linspace(-5,5, 100)

    # 50, 50, 2
    coods = torch.stack( torch.meshgrid( linspace, linspace ), dim=-1)
    density = nerf(coods)
    density = density.detach().numpy()

    ax.pcolormesh(coods[...,0],coods[...,1],  density * 0.9, cmap = cm.binary, shading='auto', vmin = 0, vmax=1)

class System:
    def __init__(self, start_state, end_state, steps):
        self.dt = 0.1

        self.start_state = start_state[None,:]
        self.end_state = end_state[None,:]

        slider = torch.linspace(0, 1, steps)[1:-1, None]

        states = (1-slider) * start_state + slider * end_state
        # self.states = torch.tensor(states, requires_grad=True)
        self.states = states.clone().detach().requires_grad_(True)

        body = torch.stack( torch.meshgrid( torch.linspace(-0.5, 0.5, 10), 
                                            torch.linspace(-  1,   1, 10) ), dim=-1)

        self.robot_body = body.reshape(-1, 2)

    def params(self):
        return [self.states]

    def get_states(self):
        return torch.cat( [self.start_state, self.states, self.end_state], dim=0)

    def get_actions(self):
        states = self.get_states()
        prev_state = states[:-1, :]
        next_state = states[1:, :]

        middle_rot = (prev_state[:, 2] + next_state[:,2])/2
        rot_matrix = self.rot_matrix(-middle_rot) # inverse because world -> body

        lin_vel = rot_matrix @ (next_state[:, :2] - prev_state[:, :2])[...,None] / self.dt
        lin_vel = lin_vel[...,0]

        rot_vel = (next_state[:, 2:] - prev_state[:,2:])/self.dt

        return torch.cat( [lin_vel, rot_vel], dim=-1 )


    @typechecked
    def body_to_world(self, points: TensorType["batch", 2]) -> TensorType["states", "batch", 2]:
        states = self.get_states()
        pos = states[..., :2]
        rot = states[..., 2]

        # S, 2, P      S, 2, 2               2, P       S, 2, _
        world_points = self.rot_matrix(rot) @ points.T + pos[..., None]
        return world_points.swapdims(-1,-2)


    def get_cost(self):
        actions = self.get_actions()

        # loss function helps get the smooth animation (vs evacuating the 
        # high density region immediately
        x = actions[:, 0]**4
        y = actions[:, 1]**4
        a = actions[:, 2]**4
        distance = (x**2 + y**2)**0.5 * self.dt

        density = nerf( self.body_to_world(self.robot_body)[1:,...] )**2

        colision_prob = torch.mean( density, dim = -1) * distance

        return y*10 + a*0.1 + 0.01*x + colision_prob * 10

    def total_cost(self):
        return torch.sum(self.get_cost())


    @staticmethod
    @typechecked
    def rot_matrix(angle: TensorType["batch":...]) -> TensorType["batch":..., 2, 2]:
        rot_matrix = torch.zeros( angle.shape + (2,2) )
        rot_matrix[:, 0,0] =  torch.cos(angle)
        rot_matrix[:, 0,1] = -torch.sin(angle)
        rot_matrix[:, 1,0] =  torch.sin(angle)
        rot_matrix[:, 1,1] =  torch.cos(angle)
        return rot_matrix

    def plot(self, fig = None):
        if fig == None:
            fig = plt.figure(figsize=plt.figaspect(2.))
        ax_map = fig.add_subplot(2, 1, 1)
        ax_graph = fig.add_subplot(2, 1, 2)
        self.plot_map(ax_map)
        plot_nerf(ax_map, nerf)

        self.plot_graph(ax_graph) 
        plt.show()

    def plot_graph(self, ax):
        states = self.get_states().detach().numpy()
        ax.plot(states[...,0], label="x")
        ax.plot(states[...,1], label="y")
        ax.plot(states[...,2], label="a")
        actions = self.get_actions().detach().numpy() 
        ax.plot(actions[...,0], label="dx")
        ax.plot(actions[...,1], label="dy")
        ax.plot(actions[...,2], label="da")

        ax_right = ax.twinx()
        ax_right.plot(self.get_cost().detach().numpy(), label="cost")
        ax.legend()

    def plot_map(self, ax, color = "g", show_cloud = True, alpha = 1):
        ax.set_aspect('equal')
        ax.set_xlim(-2, 5)
        ax.set_ylim(-2, 5)
        # ax.set_xlim(-5, 5)
        # ax.set_ylim(-5, 5)

        # PLOT PATH
        # S, 1, 2
        pos = self.body_to_world( torch.zeros((1,2))).detach().numpy()
        ax.plot( * pos.T , alpha = alpha)

        if show_cloud:
            # PLOTS BODY POINTS
            # S, P, 2
            body_points = self.body_to_world(self.robot_body).detach().numpy()
            for state_body in body_points:
                # ax.plot( *state_body.T, color+".", ms=72./ax.figure.dpi, alpha = 0.5*alpha)
                ax.plot( *state_body.T, color+".", ms=72./ax.figure.dpi, alpha = alpha)

        # PLOTS AXIS
        # if show_cloud:
        #     size = 0.5
        #     points = torch.tensor( [[0, 0], [size, 0], [0, size]])
        #     colors = ["r", "b"]

        #     # S, 3, 2
        #     points_world_frame = self.body_to_world(points).detach().numpy()
        #     for state_axis in points_world_frame:
        #         for i in range(1, 3):
        #             ax.plot(state_axis[[0,i], 0],
        #                     state_axis[[0,i], 1],
        #                     c=colors[i - 1], alpha = alpha)

        # body = torch.stack( torch.meshgrid( torch.linspace(-0.5, 0.5, 10), 
        #                                     torch.linspace(-  1,   1, 10) ), dim=-1)

        #plot box
        points = torch.tensor( [[-0.5, -1], [-0.5, 1], [0.5, 1], [0.5, -1]])
        points_world_frame = self.body_to_world(points).detach().numpy()
        for state_axis in points_world_frame:
            for i in range(4):
                ax.plot(state_axis[[i,(i+1)%4], 0],
                        state_axis[[i,(i+1)%4], 1],
                        c=color, alpha = alpha)


def main(option = "figure"):
    start_state = torch.tensor([4,0,0])
    # end_state   = torch.tensor([3,3, np.pi/2])
    # end_state   = torch.tensor([3,3, 0])
    end_state   = torch.tensor([0,4, 0.01])

    steps = 20

    traj = System(start_state, end_state, steps)

    opt = torch.optim.Adam(traj.params(), lr=0.05)

    if option == "figure":
        fig = plt.figure(figsize=plt.figaspect(1.))
        ax_map = fig.add_subplot(1, 1, 1)

    for it in range(1200):
        opt.zero_grad()
        loss = traj.total_cost()
        print(it, loss)
        loss.backward()

        if option == "figure":
            if it ==   0: traj.plot_map(ax_map, color = "r",show_cloud = False,  alpha = 0.35)
            # if it == 100: traj.plot_map(ax_map, color = "y", alpha = 0.5)
            if it == 400: traj.plot_map(ax_map, color = "b", show_cloud = False, alpha = 0.35)
            # if it == 500: traj.plot_map(ax_map, color = "b", alpha = 0.5)

        elif option == "gif":
            if it % 10 == 0:
                fig = plt.figure(figsize=plt.figaspect(1.))
                ax_map = fig.add_subplot(1, 1, 1)
                plot_nerf(ax_map, nerf)
                traj.plot_map(ax_map, color = "g",show_cloud = True,  alpha = 0.7)
                #need to make folder for this
                fig.savefig( "piano_gif_testing/" + str(it//10) + ".png")


        opt.step()

    if option == "figure":
        plot_nerf(ax_map, nerf)
        traj.plot_map(ax_map)
        plt.show()



if __name__ == "__main__":
    main("figure")
    # main("gif")
