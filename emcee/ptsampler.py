#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import (division, print_function, absolute_import,
                        unicode_literals)

__all__ = ["PTSampler", "PTState", "default_beta_ladder"]

import numpy as np
import numpy.random as nr
import multiprocessing as multi

from . import autocorr
from .sampler import Sampler

def default_beta_ladder(ndim, ntemps=None, Tmax=None, include_inf=False):
    """Returns a ladder of :math:`\beta \equiv 1/T` with temperatures
    geometrically spaced with spacing chosen so that a Gaussian
    posterior would have a 0.25 temperature swap acceptance rate.

    :param ndim:
        The number of dimensions in the parameter space.

    :param ntemps: (optional)
        If set, the number of temperatures to use.  If ``None``, the
        ``Tmax`` argument must be given, and the number of
        temperatures is chosen so that the highest temperature is
        greater than ``Tmax``.

    :param Tmax: (optional)
        If ``ntemps`` is not given, this argument controls the number
        of temperatures.  Temperatures are chosen according to the
        spacing criteria until the maximum temperature exceeds
        ``Tmax``

    """
    tstep = np.array([25.2741, 7., 4.47502, 3.5236, 3.0232,
                      2.71225, 2.49879, 2.34226, 2.22198, 2.12628,
                      2.04807, 1.98276, 1.92728, 1.87946, 1.83774,
                      1.80096, 1.76826, 1.73895, 1.7125, 1.68849,
                      1.66657, 1.64647, 1.62795, 1.61083, 1.59494,
                      1.58014, 1.56632, 1.55338, 1.54123, 1.5298,
                      1.51901, 1.50881, 1.49916, 1.49, 1.4813,
                      1.47302, 1.46512, 1.45759, 1.45039, 1.4435,
                      1.4369, 1.43056, 1.42448, 1.41864, 1.41302,
                      1.40761, 1.40239, 1.39736, 1.3925, 1.38781,
                      1.38327, 1.37888, 1.37463, 1.37051, 1.36652,
                      1.36265, 1.35889, 1.35524, 1.3517, 1.34825,
                      1.3449, 1.34164, 1.33847, 1.33538, 1.33236,
                      1.32943, 1.32656, 1.32377, 1.32104, 1.31838,
                      1.31578, 1.31325, 1.31076, 1.30834, 1.30596,
                      1.30364, 1.30137, 1.29915, 1.29697, 1.29484,
                      1.29275, 1.29071, 1.2887, 1.28673, 1.2848,
                      1.28291, 1.28106, 1.27923, 1.27745, 1.27569,
                      1.27397, 1.27227, 1.27061, 1.26898, 1.26737,
                      1.26579, 1.26424, 1.26271, 1.26121,
                      1.25973])
    dmax = tstep.shape[0]

    if ndim > dmax:
        # An approximation to the temperature step at large
        # dimension
        tstep = 1.0 + 2.0*np.sqrt(np.log(4.0))/np.sqrt(ndim)
    else:
        tstep = tstep[ndim-1]

    if ntemps is None and Tmax is None:
        raise ValueError('must specify one of ``ntemps`` and ``Tmax``')
    elif ntemps is None:
        ntemps = int(np.log(Tmax)/np.log(tstep)+2)

    if include_inf:
        ntemps -= 1

    betas = np.exp(np.linspace(0, -(ntemps-1)*np.log(tstep), ntemps))
    return np.concatenate((betas, [0])) if include_inf else betas

class PTState:
    """
    Class representing the run state of ``PTSampler``. Used for resuming runs.
    """
    def __init__(self, p, betas, time=0, ratios=None, alpha=alpha):
        self.time = time
        self.p = np.array(p).copy()
        self.betas = np.array(betas).copy()
        self.ratios = ratios
        self.alpha = alpha

class PTLikePrior(object):
    """
    Wrapper class for logl and logp.

    """

    def __init__(self, logl, logp, loglargs=[], logpargs=[], loglkwargs={},
                 logpkwargs={}):
        self.logl = logl
        self.logp = logp
        self.loglargs = loglargs
        self.logpargs = logpargs
        self.loglkwargs = loglkwargs
        self.logpkwargs = logpkwargs

    def __call__(self, x):
        lp = self.logp(x, *self.logpargs, **self.logpkwargs)

        if lp == float('-inf'):
            # Can't return -inf, since this messes with beta=0 behaviour.
            return 0, lp

        return self.logl(x, *self.loglargs, **self.loglkwargs), lp

class PTSampler(Sampler):
    """
    A parallel-tempered ensemble sampler, using :class:`EnsembleSampler`
    for sampling within each parallel chain.

    :param nwalkers:
        The number of ensemble walkers at each temperature.

    :param dim:
        The dimension of parameter space.

    :param logl:
        The log-likelihood function.

    :param logp:
        The log-prior function.

    :param threads: (optional)
        The number of parallel threads to use in sampling.

    :param pool: (optional)
        Alternative to ``threads``.  Any object that implements a
        ``map`` method compatible with the built-in ``map`` will do
        here.  For example, :class:`multi.Pool` will do.

    :param a: (optional)
        Proposal scale factor.

    :param loglargs: (optional)
        Positional arguments for the log-likelihood function.

    :param logpargs: (optional)
        Positional arguments for the log-prior function.

    :param loglkwargs: (optional)
        Keyword arguments for the log-likelihood function.

    :param logpkwargs: (optional)
        Keyword arguments for the log-prior function.

    :param ladder_callback: (optional)
        A function that is called when the temperature ladder is
        updates. The current ``PTSampler`` object is passed as an
        argument.

    """
    def __init__(self, nwalkers, dim, logl, logp, threads=1,
                 pool=None, a=2.0, loglargs=[], logpargs=[],
                 loglkwargs={}, logpkwargs={}, ladder_callback=None):
        self.logl = logl
        self.logp = logp
        self.a = a
        self.loglargs = loglargs
        self.logpargs = logpargs
        self.loglkwargs = loglkwargs
        self.logpkwargs = logpkwargs

        self._initialized = False
        self.nwalkers = nwalkers
        self.dim = dim

        if self.nwalkers % 2 != 0:
            raise ValueError('The number of walkers must be even.')
        if self.nwalkers < 2 * self.dim:
            raise ValueError('The number of walkers must be greater than 2*dimension.')

        self._chain = None
        self._lnprob = None
        self._lnlikelihood = None

        self.pool = pool
        if threads > 1 and pool is None:
            self.pool = multi.Pool(threads)

        self.p = None
        self.betas = None
        self.time = 0
        self.ladder_callback = ladder_callback

        self._alpha = None

    def reset(self):
        """
        Clear the ``time``, ``chain``, ``lnprobability``,
        ``lnlikelihood``,  ``acceptance_fraction``,
        ``tswap_acceptance_fraction``,
        ``tswap_acceptance_fraction_pairs``, and
        stored properties.

        """

        self.time = 0
        self._chain = None
        self._lnprob = None
        self._lnlikelihood = None
        self._initialize(self.ntemps)
        self._ratios = None
        self._alpha = None

    def _initialize(self, ntemps):
        """
        Initialize fields that require ``ntemps``.

        """
        self.nswap = np.zeros(ntemps, dtype=np.float)
        self.nswap_accepted = np.zeros(ntemps, dtype=np.float)

        self.nswap_pairs = np.zeros(ntemps - 1, dtype=np.float)
        self.nswap_pairs_accepted = np.zeros(ntemps - 1, dtype=np.float)

        self.nswap_pairs_old = np.zeros(ntemps - 1, dtype=np.float)
        self.nswap_pairs_old_accepted = np.zeros(ntemps - 1, dtype=np.float)

        self.nprop = np.zeros((ntemps, self.nwalkers), dtype=np.float)
        self.nprop_accepted = np.zeros((ntemps, self.nwalkers),
                                       dtype=np.float)

        self._initialized = True

    def get_state(self):
        """
        Gets a ``PTState`` object representing the current state of the sampler.

        """
        return PTState(time=self.time, p=self.p, betas=self.betas, ratios=self._ratios, alphs=self._alpha)

    def sample(self, p0=None, betas=None, ntemps=None, Tmax=None, state=None, lnprob0=None, lnlike0=None,
               iterations=1, thin=1, storechain=True, evolve_ladder=False):
        """
        Advance the chains ``iterations`` steps as a generator.

        :param p0:
            The initial positions of the walkers.  Shape should be
            ``(ntemps, nwalkers, dim)``.

        :param betas: (optional)
            Array giving the inverse temperatures, :math:`\\beta=1/T`,
            used in the ladder.  The default is chosen so that a Gaussian
            posterior in the given number of dimensions will have a 0.25
            tswap acceptance rate.

        :param ntemps:
            If ``betas`` is not given, specifies the number of temperatures for
            an automatically generated ladder. Can be ``None``, in which case
            the ``Tmax`` argument sets the maximum temperature.

        :param Tmax: (optional)
            Maximum temperature for the ladder.  If ``ntemps`` is
            ``None``, this argument is used to set the temperature ladder.

        :param state: (optional)
            A ``PTState`` object containing the state of a previous run,
            from which to resume.  If given, ``p0`` and ``betas`` are ignored.

        :param lnprob0: (optional)
            The initial posterior values for the ensembles.  Shape
            ``(ntemps, nwalkers)``.

        :param lnlike0: (optional)
            The initial likelihood values for the ensembles.  Shape
            ``(ntemps, nwalkers)``.

        :param iterations: (optional)
            The number of iterations to preform.

        :param thin: (optional)
            The number of iterations to perform between saving the
            state to the internal chain.

        :param storechain: (optional)
            If ``True`` store the iterations in the ``chain``
            property.

        :param evolve_ladder: (optional)
            If ``True``, the temperature ladder is dynamically adjusted as the
            sampler runs.

        At each iteration, this generator yields

        * ``p``, the current position of the walkers.

        * ``lnprob`` the current posterior values for the walkers.

        * ``lnlike`` the current likelihood values for the walkers.

        """

        # Set up sampler state.
        if state is not None:
            self.p = state.p.copy()
            self.betas = state.betas.copy()
            self.time = state.time
            self.ratios = state.ratios
            self.alpha = state.alpha
        else:
            # Set initial walker positions.
            if p0 is not None:
                self.p = np.array(p0).copy()
            elif self.p is None:
                raise ValueError('Initial walker positions not specified.')

            # Set temperature ladder.  Only allow ntemps or Tmax if ladder is being evolved.  Append
            # beta=0 to generated ladder.
            if not evolve_ladder and betas is not None:
                self.betas = np.array(betas).copy()
            elif ntemps is not None or Tmax is not None:
                self.betas = default_beta_ladder(self.dim, ntemps=ntemps, Tmax=Tmax, include_inf=True)
            elif self.betas is None:
                raise ValueError('Temperature ladder not specified.')

            self.betas[::-1].sort()

        if not self._initialized:
            self._initialize(self.ntemps)

        mapf = map if self.pool is None else self.pool.map
        betas = self.betas.reshape((-1, 1))

        # If we have no lnprob or logls compute them
        if lnprob0 is None or lnlike0 is None:
            fn = PTLikePrior(self.logl, self.logp, self.loglargs,
                             self.logpargs, self.loglkwargs, self.logpkwargs)
            results = list(mapf(fn, self.p.reshape((-1, self.dim))))

            logls = np.array([r[0] for r in results]).reshape((self.ntemps,
                                                               self.nwalkers))
            logps = np.array([r[1] for r in results]).reshape((self.ntemps,
                                                               self.nwalkers))

            lnlike0 = logls
            lnprob0 = logls * betas + logps

        lnprob = lnprob0
        logl = lnlike0
        logp = logps

        # Expand the chain in advance of the iterations
        if storechain:
            isave = self._update_chain(iterations / thin)

        for i in range(iterations):
            for j in [0, 1]:
                # Get positions of walkers to be updated and walker to be sampled.
                jupdate = j
                jsample = (j + 1) % 2
                pupdate = self.p[:, jupdate::2, :]
                psample = self.p[:, jsample::2, :]

                zs = np.exp(np.random.uniform(low=-np.log(self.a), high=np.log(self.a), size=(self.ntemps, self.nwalkers//2)))

                qs = np.zeros((self.ntemps, self.nwalkers//2, self.dim))
                for k in range(self.ntemps):
                    js = np.random.randint(0, high=self.nwalkers // 2,
                                           size=self.nwalkers // 2)
                    qs[k, :, :] = psample[k, js, :] + zs[k, :].reshape(
                        (self.nwalkers // 2, 1)) * (pupdate[k, :, :] -
                                                   psample[k, js, :])

                fn = PTLikePrior(self.logl, self.logp, self.loglargs,
                                 self.logpargs, self.loglkwargs,
                                 self.logpkwargs)
                results = list(mapf(fn, qs.reshape((-1, self.dim))))

                qslogls = np.array([r[0] for r in results]).reshape(
                    (self.ntemps, self.nwalkers//2))
                qslogps = np.array([r[1] for r in results]).reshape(
                    (self.ntemps, self.nwalkers/2))
                qslnprob = qslogls * betas + qslogps

                logpaccept = self.dim*np.log(zs) + qslnprob \
                    - lnprob[:, jupdate::2]
                logrs = np.log(np.random.uniform(low=0.0, high=1.0,
                                                 size=(self.ntemps,
                                                       self.nwalkers//2)))

                accepts = logrs < logpaccept
                accepts = accepts.flatten()

                pupdate.reshape((-1, self.dim))[accepts, :] = \
                    qs.reshape((-1, self.dim))[accepts, :]
                lnprob[:, jupdate::2].reshape((-1,))[accepts] = \
                    qslnprob.reshape((-1,))[accepts]
                logl[:, jupdate::2].reshape((-1,))[accepts] = \
                    qslogls.reshape((-1,))[accepts]
                logp[:, jupdate::2].reshape((-1,))[accepts] = \
                    qslogps.reshape((-1,))[accepts]

                accepts = accepts.reshape((self.ntemps, self.nwalkers//2))

                self.nprop[:, jupdate::2] += 1.0
                self.nprop_accepted[:, jupdate::2] += accepts

            self.p, lnprob, logl = self._temperature_swaps(self.p, lnprob, logl)

            if evolve_ladder:
                dbetas = self._evolve_ladder()
                if dbetas is not None:
                    self.betas += dbetas
                    lnprob += dbetas.reshape((-1, 1)) * logl

                    if callable(self.ladder_callback):
                        self.ladder_callback(self)

                self.nswap_pairs_old = self.nswap_pairs.copy()
                self.nswap_pairs_old_accepted = self.nswap_pairs_accepted.copy()

            # Check that posterior is correct.
            values = lnprob - (betas * logl + logp)
            condition = np.abs(values) < 1e-10
            assert condition.all(), \
                    'Posterior does not match likelihood and prior at step {:}: {:}'.format(i, values[np.logical_not(condition)])

            if (i + 1) % thin == 0:
                if storechain:
                    self._chain[:, :, isave, :] = self.p
                    self._lnprob[:, :, isave, ] = lnprob
                    self._lnlikelihood[:, :, isave] = logl
                    isave += 1

            yield self.p, lnprob, logl
            self.time += 1

    def _temperature_swaps(self, p, lnprob, logl):
        """
        Perform parallel-tempering temperature swaps on the state
        in ``p`` with associated ``lnprob`` and ``logl``.

        """
        ntemps = self.ntemps

        ratios = np.zeros(ntemps - 1)
        for i in range(ntemps - 1, 0, -1):
            bi = self.betas[i]
            bi1 = self.betas[i - 1]

            dbeta = bi1 - bi

            iperm = nr.permutation(self.nwalkers)
            i1perm = nr.permutation(self.nwalkers)

            raccept = np.log(nr.uniform(size=self.nwalkers))
            paccept = dbeta * (logl[i, iperm] - logl[i - 1, i1perm])

            self.nswap[i] += self.nwalkers
            self.nswap[i - 1] += self.nwalkers

            self.nswap_pairs[i - 1] += self.nwalkers

            asel = (paccept > raccept)
            nacc = np.sum(asel)

            self.nswap_accepted[i] += nacc
            self.nswap_accepted[i - 1] += nacc

            self.nswap_pairs_accepted[i - 1] += nacc
            ratios[i - 1] = nacc / self.nwalkers

            ptemp = np.copy(p[i, iperm[asel], :])
            ltemp = np.copy(logl[i, iperm[asel]])
            prtemp = np.copy(lnprob[i, iperm[asel]])

            p[i, iperm[asel], :] = p[i - 1, i1perm[asel], :]
            logl[i, iperm[asel]] = logl[i - 1, i1perm[asel]]
            lnprob[i, iperm[asel]] = lnprob[i - 1, i1perm[asel]] \
                - dbeta * logl[i - 1, i1perm[asel]]

            p[i - 1, i1perm[asel], :] = ptemp
            logl[i - 1, i1perm[asel]] = ltemp
            lnprob[i - 1, i1perm[asel]] = prtemp + dbeta * ltemp

        if self._ratios is None or self._alpha is None:
            self._ratios = ratios
        else:
            self._ratios = self._alpha * ratios + (1 - self._alpha) * self._ratios

        return p, lnprob, logl

    def _evolve_ladder(self):
        # Some sanity checks on the ladder...
        assert np.all(np.diff(self.betas) < 1), \
                'Temperatures should be in ascending order.'
        assert self.betas[0] == 1, \
                'Bottom temperature should be 1.'
        assert self.betas[-1] == 0, \
                'Top temperature should be inf.'

        betas = self.betas.copy()

        # Don't allow chains to move by more than 45% of the log spacing to the adjacent one (to
        # avoid collisions).
        lag = 50000
        a = 0.45
        kappa0 = a * lag / (self.time + lag)
        As = self._ratios

        # Ignore topmost chain, since it's at T=infinity.
        loggammas = -np.diff(np.log(betas[:-1]))

        kappa = np.zeros(len(betas))
        dlogbetas = np.zeros(len(betas))

        # Drive the non-extremal chains toward even spacing.
        dlogbetas[1:-1] = -(As[:-1] - As[1:])

        # Calculate dynamics time-scale (kappa). Limit the adjustment of log(beta) to less than half
        # the size of the gap in the direction in which the chain is moving (to avoid the chains
        # bouncing off each other). If log(beta) is decreasing, chain is ascending, so use gap with
        # next-highest chain (and vice versa).
        kappa[1:-2] = np.select([ -dlogbetas[1:-2] < 0, -dlogbetas[1:-2] > 0 ],
                                [  loggammas[ :-1],      loggammas[1:  ]     ])

        # Second topmost chain should ignore gap with topmost chain (since it'll be infinite!) and
        # just use lower gap instead.
        kappa[-2] = loggammas[-1]

        # Compute new temperature spacings.
        dlogbetas *= kappa0 * kappa
        dloggammas = -np.diff(dlogbetas[:-1])
        loggammas += dloggammas

        # Determine smoothing constant for acceptance ratio accumulation.  For
        # now, use just use average of neighbouring kappas.
        self._alpha = (kappa[1:] + kappa[:-1]) / 2

        # Finally, compute the new ladder.
        betas[1:-1] = np.exp(-np.cumsum(loggammas))

        # Temperatures should still be in the correct order (and distinct), unless something has
        # gone wrong.
        assert np.all(np.diff(betas) < 0), \
                'Temperatures not correctly ordered following temperature dynamics.'

        # Don't mutate the ladder here; let the client code do that.
        return betas - self.betas

    def _expand_chain(self, nsave):
        if self._chain is None:
            isave = 0
            self._chain = np.zeros((self.ntemps, self.nwalkers, nsave,
                                    self.dim))
            self._lnprob = np.zeros((self.ntemps, self.nwalkers, nsave))
            self._lnlikelihood = np.zeros((self.ntemps, self.nwalkers,
                                           nsave))
        else:
            isave = self._chain.shape[2]
            self._chain = np.concatenate((self._chain,
                                          np.zeros((self.ntemps,
                                                    self.nwalkers,
                                                    nsave, self.dim))),
                                         axis=2)
            self._lnprob = np.concatenate((self._lnprob,
                                           np.zeros((self.ntemps,
                                                     self.nwalkers,
                                                     nsave))),
                                          axis=2)
            self._lnlikelihood = np.concatenate((self._lnlikelihood,
                                                 np.zeros((self.ntemps,
                                                           self.nwalkers,
                                                           nsave))),
                                                axis=2)

        return isave

    def thermodynamic_integration_log_evidence(self, logls=None, fburnin=0.1):
        """
        Thermodynamic integration estimate of the evidence.

        :param logls: (optional) The log-likelihoods to use for
            computing the thermodynamic evidence.  If ``None`` (the
            default), use the stored log-likelihoods in the sampler.
            Should be of shape ``(Ntemps, Nwalkers, Nsamples)``.

        :param fburnin: (optional)
            The fraction of the chain to discard as burnin samples; only the
            final ``1-fburnin`` fraction of the samples will be used to
            compute the evidence; the default is ``fburnin = 0.1``.

        :return ``(lnZ, dlnZ)``: Returns an estimate of the
            log-evidence and the error associated with the finite
            number of temperatures at which the posterior has been
            sampled.

        The evidence is the integral of the un-normalized posterior
        over all of parameter space:

        .. math::

            Z \\equiv \\int d\\theta \\, l(\\theta) p(\\theta)

        Thermodymanic integration is a technique for estimating the
        evidence integral using information from the chains at various
        temperatures.  Let

        .. math::

            Z(\\beta) = \\int d\\theta \\, l^\\beta(\\theta) p(\\theta)

        Then

        .. math::

            \\frac{d \\ln Z}{d \\beta}
            = \\frac{1}{Z(\\beta)} \\int d\\theta l^\\beta p \\ln l
            = \\left \\langle \\ln l \\right \\rangle_\\beta

        so

        .. math::

            \\ln Z(\\beta = 1)
            = \\int_0^1 d\\beta \\left \\langle \\ln l \\right\\rangle_\\beta

        By computing the average of the log-likelihood at the
        difference temperatures, the sampler can approximate the above
        integral.
        """

        if logls is None:
            return self.thermodynamic_integration_log_evidence(
                logls=self.lnlikelihood, fburnin=fburnin)
        else:
            betas = np.concatenate((self.betas, np.array([0])))
            betas2 = np.concatenate((self.betas[::2], np.array([0])))

            istart = int(logls.shape[2] * fburnin + 0.5)

            mean_logls = np.mean(np.mean(logls, axis=1)[:, istart:], axis=1)
            mean_logls2 = mean_logls[::2]

            lnZ = -np.dot(mean_logls, np.diff(betas))
            lnZ2 = -np.dot(mean_logls2, np.diff(betas2))

            return lnZ, np.abs(lnZ - lnZ2)

    @property
    def chain(self):
        """
        Returns the stored chain of samples; shape ``(Ntemps,
        Nwalkers, Nsteps, Ndim)``.

        """
        return self._chain

    @property
    def flatchain(self):
        """Returns the stored chain, but flattened along the walker axis, so
        of shape ``(Ntemps, Nwalkers*Nsteps, Ndim)``.

        """

        s = self.chain.shape

        return self._chain.reshape((s[0], -1, s[3]))

    @property
    def lnprobability(self):
        """
        Matrix of lnprobability values; shape ``(Ntemps, Nwalkers, Nsteps)``.

        """
        return self._lnprob

    @property
    def lnlikelihood(self):
        """
        Matrix of ln-likelihood values; shape ``(Ntemps, Nwalkers, Nsteps)``.

        """
        return self._lnlikelihood

    @property
    def tswap_acceptance_fraction(self):
        """
        Returns an array of accepted temperature swap fractions for
        each temperature; shape ``(ntemps, )``.

        """
        return self.nswap_accepted / self.nswap

    @property
    def tswap_acceptance_fraction_pairs(self):
        """
        Returns an array of accepted temperature swap fractions for
        each temperature; shape ``(ntemps, )``.

        """
        return self.nswap_pairs_accepted / self.nswap_pairs

    @property
    def ntemps(self):
        """
        The number of temperature chains.

        """
        return len(self.betas)

    @property
    def acceptance_fraction(self):
        """
        Matrix of shape ``(Ntemps, Nwalkers)`` detailing the
        acceptance fraction for each walker.

        """
        return self.nprop_accepted / self.nprop

    @property
    def acor(self):
        """
        Returns a matrix of autocorrelation lengths for each
        parameter in each temperature of shape ``(Ntemps, Ndim)``.

        """
        return self.get_autocorr_time()

    def get_autocorr_time(self, window=50):
        """
        Returns a matrix of autocorrelation lengths for each
        parameter in each temperature of shape ``(Ntemps, Ndim)``.

        :param window: (optional)
            The size of the windowing function. This is equivalent to the
            maximum number of lags to use. (default: 50)

        """
        acors = np.zeros((self.ntemps, self.dim))

        for i in range(self.ntemps):
            x = np.mean(self._chain[i, :, :, :], axis=0)
            acors[i, :] = autocorr.integrated_time(x, window=window)
        return acors
