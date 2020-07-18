import torch
from torch import nn
from plnn.proxlp_solver.solver import SaddleLP
from plnn.proxlp_solver import utils
import math, time


default_params = {
    'nb_steps': 100,
    'outer_cutoff': None,
    'initial_step_size': 1e-3,
    'final_step_size': 1e-6,
    'betas': (0.9, 0.999),
}


class DJRelaxationLP(SaddleLP):

    def __init__(self, layers, params=None, store_bounds_progress=-1, store_bounds_primal=False, max_batch=2000):
        """
        :param store_obj_progress: which proximal iteration to store objective progress for. None=don't.
        :param store_bounds_primal: whether to store the primal solution used to compute the final bounds
        """
        self.layers = layers
        self.net = nn.Sequential(*layers)
        self.params = dict(default_params, **params) if params is not None else default_params

        for param in self.net.parameters():
            param.requires_grad = False
        self.store_bounds_progress = store_bounds_progress
        # Store dict of lists of tensors containing the progress in the bounds with the inner iters.
        self.bounds_progress_per_layer = {}
        self.store_bounds_primal = store_bounds_primal
        self.max_batch = max_batch

        # by default, there is no external initialization, initialize w/ interval propagation (all 0s)
        self.external_init_dual = None

        self.optimize = self.dj_adam_optimizer

    def dj_adam_optimizer(self, weights, additional_coeffs, lower_bounds, upper_bounds):

        nb_steps = self.params['nb_steps']
        initial_step_size = self.params['initial_step_size']
        final_step_size = self.params['final_step_size']
        beta_1 = self.params['betas'][0]
        beta_2 = self.params['betas'][1]
        outer_cutoff = self.params['outer_cutoff']
        use_cutoff = (outer_cutoff is not None) and outer_cutoff > 0

        start_time = time.time()
        # Initialise entry of dictionary storing progress in objective function.
        self.bounds_progress_per_layer[len(weights)] = []
        self.bounds_progress_per_layer[-len(weights)] = []  # negative key stores iter number

        if self.external_init_dual is None:
            # there is no external initialization, initialize w/ interval propagation (all 0s)
            dual_vars = DualVarSet.naive_initialization(weights, additional_coeffs, lower_bounds)
        else:
            # initialize from externally set dual solution
            dual_vars = self.external_init_dual

        exp_avg = dual_vars.zero_like()
        exp_avg_sq = dual_vars.zero_like()

        # create clamped post-activation bounds
        l_postacts = [lower_bounds[0]] + [torch.clamp(bound, 0, None) for bound in lower_bounds[1:]]  # 0 to n-1
        u_postacts = [upper_bounds[0]] + [torch.clamp(bound, 0, None) for bound in upper_bounds[1:]]  # 0 to n-1

        if use_cutoff:
            matching_primal = get_optim_primal(weights, additional_coeffs, lower_bounds, upper_bounds, l_postacts,
                                               u_postacts, dual_vars)
            old_bound = compute_objective(weights, additional_coeffs, matching_primal, dual_vars)
            diff_avg = torch.zeros_like(old_bound)

        for step in range(1, nb_steps + 1):
            matching_primal = get_optim_primal(weights, additional_coeffs, lower_bounds, upper_bounds, l_postacts,
                                               u_postacts, dual_vars)

            dual_subg = matching_primal.as_dual_subgradient(weights)

            step_size = initial_step_size + (step / nb_steps) * (final_step_size - initial_step_size)

            bias_correc1 = 1 - beta_1 ** step
            bias_correc2 = 1 - beta_2 ** step

            # Decay the first and second moment running average coefficient
            exp_avg.mul_(beta_1).add_(1 - beta_1, dual_subg)
            exp_avg_sq.mul_(beta_2).addcmul_(1 - beta_2, dual_subg, dual_subg)
            denom = (exp_avg_sq.sqrt().div_cte_(math.sqrt(bias_correc2))).add_cte_(1e-8)

            step_size = step_size / bias_correc1

            dual_vars.addcdiv_(step_size, exp_avg, denom)

            if self.store_bounds_progress >= 0 and len(weights) == self.store_bounds_progress:
                if (step - 1) % 10 == 0:
                    matching_primal = get_optim_primal(weights, additional_coeffs, lower_bounds, upper_bounds,
                                                       l_postacts, u_postacts, dual_vars)
                    bound = compute_objective(weights, additional_coeffs, matching_primal, dual_vars)
                    self.bounds_progress_per_layer[len(weights)].append(bound)
                    self.bounds_progress_per_layer[-len(weights)].append(
                        time.time() - start_time)  # negative key stores elapsed time

            # Stop outer iterations if improvement in bounds (running average of bounds diff) is small.
            if use_cutoff:
                matching_primal = get_optim_primal(weights, additional_coeffs, lower_bounds, upper_bounds, l_postacts,
                                                   u_postacts, dual_vars)
                bound = compute_objective(weights, additional_coeffs, matching_primal, dual_vars)
                diff_avg = 0.5 * diff_avg + 0.5 * (bound - old_bound)
                old_bound = bound.clone()
                if diff_avg.mean() < outer_cutoff and step > 10:
                    print(
                        f"Breaking inner optimization after {step} iterations, decrease {diff_avg.mean()}")
                    break

        # store last dual solution for future usage
        self.last_duals = [(lambdak, muk) for lambdak, muk in zip(dual_vars.lambdas, dual_vars.mus)]

        # End of the optimization
        matching_primal = get_optim_primal(weights, additional_coeffs, lower_bounds, upper_bounds, l_postacts,
                                           u_postacts, dual_vars)
        if self.store_bounds_primal:
            self.bounds_primal = matching_primal
        bound = compute_objective(weights, additional_coeffs, matching_primal, dual_vars)

        return bound

    def get_lower_bound_network_input(self):
        """
        Return the input of the network that was used in the last bounds computation.
        Converts back from the conditioned input domain to the original one.
        Assumes that the last layer is a single neuron.
        """
        assert self.store_bounds_primal
        assert self.bounds_primal.xs[0].shape[1] == 2, "the last layer must have a single neuron"
        l_0 = self.input_domain.select(-1, 0)
        u_0 = self.input_domain.select(-1, 1)
        net_input = (1/2) * (u_0 - l_0) * self.bounds_primal.xs[0].select(1, 1) + (1/2) * (u_0 + l_0)
        return net_input

    def initialize_dual_from(self, external_init):
        # setter to have the optimizer initialized from an external list of dual variables (as list of tuples of
        # tensors like (lambdas, mus))
        lambdas = []
        mus = []
        for lambdak, muk in external_init:
            lambdas.append(lambdak)
            mus.append(muk)
        self.external_init_dual = DualVarSet(lambdas, mus)

def get_optim_primal(weights, additional_coeffs, lower_bounds, upper_bounds, l_postacts, u_postacts, dual_vars):
    """
    Given the network layers (LinearOp and ConvOp classes in proxlp_solver.utils), cost coefficients of the final layer,
    primal and dual variables (PrimalVarSet and DualVarSet, respectively), pre and post activation bounds
    (lists of tensors), compute the primal variables at the argmin of the inner problem.
    :return: optimal primals as PrimalVarSet
    """
    add_coeff = next(iter(additional_coeffs.values()))

    xs_opt = []
    for x_idx in range(len(weights)):
        if x_idx == 0:
            xs_opt.append(torch.where(weights[x_idx].backward(dual_vars.mus[x_idx]) >= 0, u_postacts[x_idx].unsqueeze(1),
                                      l_postacts[x_idx].unsqueeze(1)))
        elif x_idx == len(weights) - 1:
            xs_opt.append(
                torch.where(dual_vars.lambdas[x_idx - 1] + weights[x_idx].backward(add_coeff) <= 0,
                            u_postacts[x_idx].unsqueeze(1), l_postacts[x_idx].unsqueeze(1)))
        else:
            xs_opt.append(torch.where(dual_vars.lambdas[x_idx-1] - weights[x_idx].backward(dual_vars.mus[x_idx]) <= 0,
                                      u_postacts[x_idx].unsqueeze(1), l_postacts[x_idx].unsqueeze(1)))

    zs_opt = []
    for x_idx in range(len(weights)-1):
        # for non-ambiguous relus, the min can be at the lower or upper bound of z
        lower_gk = dual_vars.mus[x_idx] * lower_bounds[x_idx+1].unsqueeze(1) - \
            dual_vars.lambdas[x_idx] * torch.clamp(lower_bounds[x_idx+1].unsqueeze(1), 0, None)
        upper_gk = dual_vars.mus[x_idx] * upper_bounds[x_idx+1].unsqueeze(1) - \
            dual_vars.lambdas[x_idx] * torch.clamp(upper_bounds[x_idx+1].unsqueeze(1), 0, None)
        zk = torch.where(lower_gk <= upper_gk, lower_bounds[x_idx+1].unsqueeze(1), upper_bounds[x_idx+1].unsqueeze(1))

        # for ambiguous relus, we need to check at 0 as well
        zero_gk = torch.zeros_like(lower_gk)
        zk = torch.where((lower_bounds[x_idx+1].unsqueeze(1) < 0) & (upper_bounds[x_idx+1].unsqueeze(1) > 0) &
                         (zero_gk < torch.min(lower_gk, upper_gk)), zero_gk, zk)
        zs_opt.append(zk)

    return PrimalVarSet(xs_opt, zs_opt)


def compute_objective(weights, additional_coeffs, primal_vars, dual_vars):
    """
    Given the network layers (LinearOp and ConvOp classes in proxlp_solver.utils), cost coefficients of the final layer,
    primal and dual variables (PrimalVarSet and DualVarSet, respectively), compute the objective function value for this
    derivation. It is equivalent to computing the bounds.
    :return: bound tensor, 2*opt_layer_width (first half is negative of upper bounds, second half is lower bounds)
    """
    add_coeff = next(iter(additional_coeffs.values()))
    obj = utils.bdot(weights[-1].backward(add_coeff), primal_vars.xs[-1]) + \
          utils.bdot(add_coeff, weights[-1].bias)

    for x_idx in range(len(weights)-1):
        obj += utils.bdot(dual_vars.mus[x_idx], primal_vars.zs[x_idx] - weights[x_idx].forward(primal_vars.xs[x_idx]))
        obj += utils.bdot(dual_vars.lambdas[x_idx],
                          primal_vars.xs[x_idx + 1] - torch.clamp(primal_vars.zs[x_idx], 0, None))
    return obj


class PrimalVarSet:
    """
    Class representing the primal variables for this derivation: pre-activation z's (includes the output layer)
    and post-activation x's (includes the input layer).
    """
    def __init__(self, xs, zs):
        self.xs = xs  # from layer 0 to n-1
        self.zs = zs  # from layer 1 to n-1

    def as_dual_subgradient(self, weights):
        # compute the subgradient of the dual variables, given the inner minimum over the primal variables (self)
        lambda_eq = []
        mu_eq = []
        for x_idx in range(len(weights)-1):
            mu_eq.append(self.zs[x_idx] - weights[x_idx].forward(self.xs[x_idx]))
            lambda_eq.append(self.xs[x_idx+1] - torch.clamp(self.zs[x_idx], 0, None))
        return DualVarSet(lambda_eq, mu_eq)


class DualVarSet:
    def __init__(self, lambdas, mus):
        self.lambdas = lambdas  # from relu 0 to n-1
        self.mus = mus  # from relu 0 to n-1

    @staticmethod
    def naive_initialization(weights, additional_coeffs, lower_bounds):
        """
        Given parameters from the optimize function, initialize the dual vairables and their functions as all 0s except
        some special corner cases. This is equivalent to initialising with naive interval propagation bounds.
        """
        add_coeff = next(iter(additional_coeffs.values()))
        batch_size = add_coeff.shape[:2]
        device = lower_bounds[-1].device

        lambdas = []
        mus = []
        for lay_idx in range(1, len(weights)):
            lay_shape = lower_bounds[lay_idx].shape[1:]
            mus.append(torch.zeros((*batch_size,) + lay_shape, device=device))
            lambdas.append(torch.zeros((*batch_size,) + lay_shape, device=device))

        return DualVarSet(lambdas, mus)

    def zero_like(self):
        new_lambdas = []
        new_mus = []
        for lambdak in self.lambdas:
            new_lambdas.append(torch.zeros_like(lambdak))
        for muk in self.mus:
            new_mus.append(torch.zeros_like(muk))
        return DualVarSet(new_lambdas, new_mus)

    def add_(self, coeff, to_add):
        for lambdak, addend in zip(self.lambdas, to_add.lambdas):
            lambdak.add_(coeff, addend)
        for muk, addend in zip(self.mus, to_add.mus):
            muk.add_(coeff, addend)
        return self

    def add_cte_(self, cte):
        for lambdak in self.lambdas:
            lambdak.add_(cte)
        for muk in self.mus:
            muk.add_(cte)
        return self

    def addcmul_(self, coeff, to_add1, to_add2):
        for lambdak, lambdak1, lambdak2 in zip(self.lambdas, to_add1.lambdas, to_add2.lambdas):
            lambdak.addcmul_(coeff, lambdak1, lambdak2)
        for muk, muk1, muk2 in zip(self.mus, to_add1.mus, to_add2.mus):
            muk.addcmul_(coeff, muk1, muk2)
        return self

    def addcdiv_(self, coeff, num, denom):
        for lambdak, num_lambdak, denom_lambdak in zip(self.lambdas, num.lambdas, denom.lambdas):
            lambdak.addcdiv_(coeff, num_lambdak, denom_lambdak)
        for muk, num_muk, denom_muk in zip(self.mus, num.mus, denom.mus):
            muk.addcdiv_(coeff, num_muk, denom_muk)
        return self

    def div_cte_(self, denom):
        for lambdak in self.lambdas:
            lambdak.div_(denom)
        for muk in self.mus:
            muk.div_(denom)
        return self

    def mul_(self, coeff):
        for lambdak in self.lambdas:
            lambdak.mul_(coeff)
        for muk in self.mus:
            muk.mul_(coeff)
        return self

    def sqrt(self):
        new_lambdas = [lambdak.sqrt() for lambdak in self.lambdas]
        new_mus = [muk.sqrt() for muk in self.mus]
        return DualVarSet(new_lambdas, new_mus)
