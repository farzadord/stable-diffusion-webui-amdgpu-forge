import dataclasses
import torch
import numpy as np
from scipy import stats
import math

from modules import shared

if shared.opts.sd_sampling == "A1111":
    import k_diffusion
    from k_diffusion.sampling import append_zero
    from k_diffusion import sampling
elif shared.opts.sd_sampling == "ldm patched (Comfy)":
    import ldm_patched.k_diffusion
    from ldm_patched.k_diffusion.sampling import append_zero
    from ldm_patched.k_diffusion import sampling

def to_d(x, sigma, denoised):
    """Converts a denoiser output to a Karras ODE derivative."""
    return (x - denoised) / sigma


if shared.opts.sd_sampling == "A1111":
    k_diffusion.sampling.to_d = to_d
elif shared.opts.sd_sampling == "ldm patched (Comfy)":
    ldm_patched.k_diffusion.sampling.to_d = to_d


@dataclasses.dataclass
class Scheduler:
    name: str
    label: str
    function: any

    default_rho: float = -1
    need_inner_model: bool = False
    aliases: list = None


def uniform(n, sigma_min, sigma_max, inner_model, device):
    return inner_model.get_sigmas(n).to(device)


def sgm_uniform(n, sigma_min, sigma_max, inner_model, device):
    start = inner_model.sigma_to_t(torch.tensor(sigma_max))
    end = inner_model.sigma_to_t(torch.tensor(sigma_min))
    sigs = [
        inner_model.t_to_sigma(ts)
        for ts in torch.linspace(start, end, n + 1)[:-1]
    ]
    sigs += [0.0]
    return torch.FloatTensor(sigs).to(device)

def get_sigmas_karras(n, sigma_min, sigma_max, rho=7., device='cpu'):
    rho = shared.opts.karras_rho
    return ldm_patched.k_diffusion.sampling.get_sigmas_karras(n, sigma_min, sigma_max, rho, device)

def get_sigmas_exponential(n, sigma_min, sigma_max, device='cpu'):
    shrink_factor = shared.opts.exponential_shrink_factor
    sigmas = torch.linspace(math.log(sigma_max), math.log(sigma_min), n, device=device).exp()
    sigmas = sigmas * torch.exp(shrink_factor * torch.linspace(0, 1, n, device=device))
    return append_zero(sigmas)


def get_sigmas_polyexponential(n, sigma_min, sigma_max, device='cpu'):
    rho = shared.opts.polyexponential_rho
    return ldm_patched.k_diffusion.sampling.get_sigmas_polyexponential(n, sigma_min, sigma_max, rho, device)


def get_sigmas_sinusoidal_sf(n, sigma_min, sigma_max, device='cpu'):
    sf = shared.opts.sinusoidal_sf_factor
    x = torch.linspace(0, 1, n, device=device)
    sigmas = (sigma_min + (sigma_max - sigma_min) * (1 - torch.sin(torch.pi / 2 * x)))/sigma_max
    sigmas = sigmas**sf
    sigmas = sigmas * sigma_max
    return sigmas

def get_sigmas_invcosinusoidal_sf(n, sigma_min, sigma_max, device='cpu'):
    sf = shared.opts.invcosinusoidal_sf_factor
    x = torch.linspace(0, 1, n, device=device)
    sigmas = (sigma_min + (sigma_max - sigma_min) * (0.5*(torch.cos(x * math.pi) + 1)))/sigma_max
    sigmas = sigmas**sf
    sigmas = sigmas * sigma_max
    return sigmas

def get_sigmas_react_cosinusoidal_dynsf(n, sigma_min, sigma_max, device='cpu'):
    sf = shared.opts.react_cosinusoidal_dynsf_factor
    x = torch.linspace(0, 1, n, device=device)
    sigmas = (sigma_min+(sigma_max-sigma_min)*(torch.cos(x*(torch.pi/2))))/sigma_max
    sigmas = sigmas**(sf*(n*x/n))
    sigmas = sigmas * sigma_max
    return sigmas


def get_align_your_steps_sigmas(n, sigma_min, sigma_max, device):
    # https://research.nvidia.com/labs/toronto-ai/AlignYourSteps/howto.html
    def loglinear_interp(t_steps, num_steps):
        """
        Performs log-linear interpolation of a given array of decreasing numbers.
        """
        xs = np.linspace(0, 1, len(t_steps))
        ys = np.log(t_steps[::-1])

        new_xs = np.linspace(0, 1, num_steps)
        new_ys = np.interp(new_xs, xs, ys)

        interped_ys = np.exp(new_ys)[::-1].copy()
        return interped_ys

    if shared.sd_model.is_sdxl:
        sigmas = [14.615, 6.315, 3.771, 2.181, 1.342, 0.862, 0.555, 0.380, 0.234, 0.113, 0.029]
    else:
        # Default to SD 1.5 sigmas.
        sigmas = [14.615, 6.475, 3.861, 2.697, 1.886, 1.396, 0.963, 0.652, 0.399, 0.152, 0.029]

    if n != len(sigmas):
        sigmas = np.append(loglinear_interp(sigmas, n), [0.0])
    else:
        sigmas.append(0.0)

    return torch.FloatTensor(sigmas).to(device)

def get_sigmas_ays_custom(n, sigma_min, sigma_max, device='cpu'):
    try:
        sigmas_str = shared.opts.ays_custom_sigmas
        sigmas_values = sigmas_str.strip('[]').split(',')
        sigmas = np.array([float(x.strip()) for x in sigmas_values])
        
        if n != len(sigmas):
            sigmas = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(sigmas)), sigmas)
        sigmas = np.append(sigmas, [0.0])
        return torch.FloatTensor(sigmas).to(device)
    except Exception as e:
        print(f"Error parsing custom sigmas: {e}")
        print("Falling back to default AYS sigmas")
        return get_align_your_steps_sigmas(n, sigma_min, sigma_max, device)

def kl_optimal(n, sigma_min, sigma_max, device):
    alpha_min = torch.arctan(torch.tensor(sigma_min, device=device))
    alpha_max = torch.arctan(torch.tensor(sigma_max, device=device))
    step_indices = torch.arange(n + 1, device=device)
    sigmas = torch.tan(step_indices / n * alpha_min + (1.0 - step_indices / n) * alpha_max)
    return sigmas

def simple_scheduler(n, sigma_min, sigma_max, inner_model, device):
    sigs = []
    ss = len(inner_model.sigmas) / n
    for x in range(n):
        sigs += [float(inner_model.sigmas[-(1 + int(x * ss))])]
    sigs += [0.0]
    return torch.FloatTensor(sigs).to(device)

def normal_scheduler(n, sigma_min, sigma_max, inner_model, device, sgm=False, floor=False):
    start = inner_model.sigma_to_t(torch.tensor(sigma_max))
    end = inner_model.sigma_to_t(torch.tensor(sigma_min))

    if sgm:
        timesteps = torch.linspace(start, end, n + 1)[:-1]
    else:
        timesteps = torch.linspace(start, end, n)

    sigs = []
    for x in range(len(timesteps)):
        ts = timesteps[x]
        sigs.append(inner_model.t_to_sigma(ts))
    sigs += [0.0]
    return torch.FloatTensor(sigs).to(device)

def ddim_scheduler(n, sigma_min, sigma_max, inner_model, device):
    sigs = []
    ss = max(len(inner_model.sigmas) // n, 1)
    x = 1
    while x < len(inner_model.sigmas):
        sigs += [float(inner_model.sigmas[x])]
        x += ss
    sigs = sigs[::-1]
    sigs += [0.0]
    return torch.FloatTensor(sigs).to(device)

def beta_scheduler(n, sigma_min, sigma_max, inner_model, device):
    """
    Beta scheduler, based on "Beta Sampling is All You Need" [arXiv:2407.12173] (Lee et. al, 2024)
    """
    alpha = shared.opts.beta_dist_alpha
    beta = shared.opts.beta_dist_beta
    
    total_timesteps = (len(inner_model.sigmas) - 1)
    ts = 1 - np.linspace(0, 1, n, endpoint=False)
    ts = np.rint(stats.beta.ppf(ts, alpha, beta) * total_timesteps)

    sigs = []
    last_t = -1
    for t in ts:
        if t != last_t:
            sigs += [float(inner_model.sigmas[int(t)])]
        last_t = t
    sigs += [0.0]
    return torch.FloatTensor(sigs).to(device)

def turbo_scheduler(n, sigma_min, sigma_max, inner_model, device):
    unet = inner_model.inner_model.forge_objects.unet
    timesteps = torch.flip(torch.arange(1, n + 1) * float(1000.0 / n) - 1, (0,)).round().long().clip(0, 999)
    sigmas = unet.model.model_sampling.sigma(timesteps)
    sigmas = torch.cat([sigmas, sigmas.new_zeros([1])])
    return sigmas.to(device)

def get_align_your_steps_sigmas_GITS(n, sigma_min, sigma_max, device):
    def loglinear_interp(t_steps, num_steps):
        """
        Performs log-linear interpolation of a given array of decreasing numbers.
        """
        xs = np.linspace(0, 1, len(t_steps))
        ys = np.log(t_steps[::-1])

        new_xs = np.linspace(0, 1, num_steps)
        new_ys = np.interp(new_xs, xs, ys)

        interped_ys = np.exp(new_ys)[::-1].copy()
        return interped_ys

    if shared.sd_model.is_sdxl:
        sigmas = [14.615, 4.734, 2.567, 1.529, 0.987, 0.652, 0.418, 0.268, 0.179, 0.127, 0.029]
    else:
        sigmas = [14.615, 4.617, 2.507, 1.236, 0.702, 0.402, 0.240, 0.156, 0.104, 0.094, 0.029]

    if n != len(sigmas):
        sigmas = np.append(loglinear_interp(sigmas, n), [0.0])
    else:
        sigmas.append(0.0)

    return torch.FloatTensor(sigmas).to(device)

def ays_11_sigmas(n, sigma_min, sigma_max, device='cpu'):
    def loglinear_interp(t_steps, num_steps):
        """
        Performs log-linear interpolation of a given array of decreasing numbers.
        """
        xs = np.linspace(0, 1, len(t_steps))
        ys = np.log(t_steps[::-1])

        new_xs = np.linspace(0, 1, num_steps)
        new_ys = np.interp(new_xs, xs, ys)

        interped_ys = np.exp(new_ys)[::-1].copy()
        return interped_ys

    if shared.sd_model.is_sdxl:
        sigmas = [14.615, 6.315, 3.771, 2.181, 1.342, 0.862, 0.555, 0.380, 0.234, 0.113, 0.029]
    else:
        sigmas = [14.615, 6.475, 3.861, 2.697, 1.886, 1.396, 0.963, 0.652, 0.399, 0.152, 0.029]

    if n != len(sigmas):
        sigmas = np.append(loglinear_interp(sigmas, n), [0.0])
    else:
        sigmas.append(0.0)

    return torch.FloatTensor(sigmas).to(device)

def ays_32_sigmas(n, sigma_min, sigma_max, device='cpu'):
    def loglinear_interp(t_steps, num_steps):
        """
        Performs log-linear interpolation of a given array of decreasing numbers.
        """
        xs = np.linspace(0, 1, len(t_steps))
        ys = np.log(t_steps[::-1])
        new_xs = np.linspace(0, 1, num_steps)
        new_ys = np.interp(new_xs, xs, ys)
        interped_ys = np.exp(new_ys)[::-1].copy()
        return interped_ys
    if shared.sd_model.is_sdxl:
        sigmas = [14.61500000000000000, 11.14916180000000000, 8.505221270000000000, 6.488271510000000000, 5.437074020000000000, 4.603986190000000000, 3.898547040000000000, 3.274074570000000000, 2.743965270000000000, 2.299686590000000000, 1.954485140000000000, 1.671087150000000000, 1.428781520000000000, 1.231810090000000000, 1.067896490000000000, 0.925794430000000000, 0.802908860000000000, 0.696601210000000000, 0.604369030000000000, 0.528525520000000000, 0.467733440000000000, 0.413933790000000000, 0.362581860000000000, 0.310085170000000000, 0.265189250000000000, 0.223264610000000000, 0.176538770000000000, 0.139591920000000000, 0.105873810000000000, 0.055193690000000000, 0.028773340000000000, 0.015000000000000000]
    else:
        sigmas = [14.61500000000000000, 11.23951352000000000, 8.643630810000000000, 6.647294240000000000, 5.572508620000000000, 4.716485460000000000, 3.991960650000000000, 3.519560900000000000, 3.134904660000000000, 2.792287880000000000, 2.487736280000000000, 2.216638650000000000, 1.975083510000000000, 1.779317200000000000, 1.614753350000000000, 1.465409530000000000, 1.314849000000000000, 1.166424970000000000, 1.034755470000000000, 0.915737440000000000, 0.807481690000000000, 0.712023610000000000, 0.621739000000000000, 0.530652020000000000, 0.452909600000000000, 0.374914550000000000, 0.274618190000000000, 0.201152900000000000, 0.141058730000000000, 0.066828810000000000, 0.031661210000000000, 0.015000000000000000]
    if n != len(sigmas):
        sigmas = np.append(loglinear_interp(sigmas, n), [0.0])
    else:
        sigmas.append(0.0)
    return torch.FloatTensor(sigmas).to(device)

def cosine_scheduler(n, sigma_min, sigma_max, device='cpu'):
    sf = shared.opts.cosine_sf_factor
    sigmas = torch.zeros(n, device=device)
    if n == 1:
        sigmas[0] = sigma_max ** 0.5
    else:
        for x in range(n):
            p = x / (n-1)
            C = sigma_min + 0.5*(sigma_max-sigma_min)*(1 - math.cos(math.pi*(1 - p**0.5)))
            sigmas[x] = C * sf
    return torch.cat([sigmas, sigmas.new_zeros([1])])

def cosexpblend_scheduler(n, sigma_min, sigma_max, device='cpu'):
    decay = shared.opts.cosexpblend_exp_decay
    sigmas = []
    if n == 1:
        sigmas.append(sigma_max ** 0.5)
    else:
        K = decay ** (1/(n-1))
        E = sigma_max
        for x in range(n):
            p = x / (n-1)
            C = sigma_min + 0.5*(sigma_max-sigma_min)*(1 - math.cos(math.pi*(1 - p**0.5)))
            sigmas.append(C + p * (E - C))
            E *= K
    sigmas += [0.0]
    return torch.FloatTensor(sigmas).to(device)

def phi_scheduler(n, sigma_min, sigma_max, device='cpu'):
    power = shared.opts.phi_power
    sigmas = torch.zeros(n, device=device)
    if n == 1:
        sigmas[0] = sigma_max ** 0.5
    else:
        phi = (1 + 5**0.5) / 2
        for x in range(n):
            sigmas[x] = sigma_min + (sigma_max-sigma_min)*((1-x/(n-1))**(phi**power))
    return torch.cat([sigmas, sigmas.new_zeros([1])])

def get_sigmas_laplace(n, sigma_min, sigma_max, device='cpu'):
    mu = shared.opts.laplace_mu
    beta = shared.opts.laplace_beta
    epsilon = 1e-5 # avoid log(0)
    x = torch.linspace(0, 1, n, device=device)
    clamp = lambda x: torch.clamp(x, min=sigma_min, max=sigma_max)
    lmb = mu - beta * torch.sign(0.5-x) * torch.log(1 - 2 * torch.abs(0.5-x) + epsilon)
    sigmas = clamp(torch.exp(lmb))
    return torch.cat([sigmas, sigmas.new_zeros([1])])

def get_sigmas_karras_dynamic(n, sigma_min, sigma_max, device='cpu'):
    rho = shared.opts.karras_dynamic_rho
    ramp = torch.linspace(0, 1, n, device=device)
    min_inv_rho = sigma_min ** (1 / rho)
    max_inv_rho = sigma_max ** (1 / rho)
    sigmas = torch.zeros_like(ramp)
    for i in range(n):
        sigmas[i] = (max_inv_rho + ramp[i] * (min_inv_rho - max_inv_rho)) ** (math.cos(i*math.tau/n)*2+rho) 
    return torch.cat([sigmas, sigmas.new_zeros([1])])

schedulers = [
    Scheduler('automatic', 'Automatic', None),
    Scheduler('karras', 'Karras', sampling.get_sigmas_karras, default_rho=7.0),
    Scheduler('exponential', 'Exponential', sampling.get_sigmas_exponential),
    Scheduler('polyexponential', 'Polyexponential', sampling.get_sigmas_polyexponential, default_rho=1.0),
    Scheduler('sinusoidal_sf', 'Sinusoidal SF', get_sigmas_sinusoidal_sf),
    Scheduler('invcosinusoidal_sf', 'Invcosinusoidal SF', get_sigmas_invcosinusoidal_sf),
    Scheduler('react_cosinusoidal_dynsf', 'React Cosinusoidal DynSF', get_sigmas_react_cosinusoidal_dynsf),
    Scheduler('uniform', 'Uniform', uniform, need_inner_model=True),
    Scheduler('sgm_uniform', 'SGM Uniform', sgm_uniform, need_inner_model=True, aliases=["SGMUniform"]),
    Scheduler('kl_optimal', 'KL Optimal', kl_optimal),
    Scheduler('simple', 'Simple', simple_scheduler, need_inner_model=True),
    Scheduler('normal', 'Normal', normal_scheduler, need_inner_model=True),
    Scheduler('ddim', 'DDIM', ddim_scheduler, need_inner_model=True),
    Scheduler('align_your_steps', 'Align Your Steps', get_align_your_steps_sigmas),
    Scheduler('align_your_steps_custom', 'Align Your Steps Custom', get_sigmas_ays_custom),
    Scheduler('beta', 'Beta', beta_scheduler, need_inner_model=True),
    Scheduler('turbo', 'Turbo', turbo_scheduler, need_inner_model=True),
    Scheduler('cosine', 'Cosine', cosine_scheduler),
    Scheduler('cosine-exponential blend', 'Cosine-exponential Blend', cosexpblend_scheduler),
    Scheduler('phi', 'Phi', phi_scheduler),
    Scheduler('laplace', 'Laplace', get_sigmas_laplace),
    Scheduler('karras dynamic', 'Karras Dynamic', get_sigmas_karras_dynamic),
    Scheduler('align_your_steps_GITS', 'Align Your Steps GITS', get_align_your_steps_sigmas_GITS),
    Scheduler('align_your_steps_11', 'Align Your Steps 11', ays_11_sigmas),
    Scheduler('align_your_steps_32', 'Align Your Steps 32', ays_32_sigmas),
]

schedulers_map = {**{x.name: x for x in schedulers}, **{x.label: x for x in schedulers}}
