'''
Latent Dirichlet Allocation

References:
Heinrich, Gregor. Parameter estimation for text analysis. Technical report. 2005 (revised 2009)
Minka, Thomas P. Estimating a Dirichlet distribution. 2000. (revised 2012)
'''

import math

import numpy as np
import numpy.random as nprand
import scipy as sp
import scipy.misc as spmisc
import scipy.special as spspecial

def word_iter(doc):
    '''Return an iterator over words in a document.
    :param doc: doc[w] is the number of times word w appears
    '''
    for w, count in enumerate(doc):
        for i in xrange(count):
            yield w

def sample(dist):
    '''Sample from the given distribution.
    
    :param dist: array of probabilities
    :returns: a randomly sampled integer in [0, len(dist))
    '''
    cdf = np.cumsum(dist)
    uniform = nprand.random_sample()
    try:
        result = next(n for n in range(0, len(cdf)) if cdf[n] > uniform)
    except StopIteration:
        print "%f %f" % (cdf[len(dist)-1], uniform)
    return result

def polya_iteration(ndm, nd, guess, rtol=1e-7, max_iter=25):
    '''Estimate the parameter of a dirichlet-multinomial distribution
    with num_dir draws from the dirichlet and num_out possible outcomes.
    
    :param ndm: Counts as (num_dir, num_out) array
    :param nd: Counts as (num_dir) array
    :param guess: Initial guess for dirichlet parameter
    :param rtol: Relative tolerance to achieve before stopping
    :param max_iter: Maximum number of iterations to allow
    :returns: An updated estimate of the dirichlet parameter
    '''
    num_dir, num_out = ndm.shape
    param = guess
    for i in range(max_iter):
        # Heinrich2005 Eq. 83
        # Minka2000 Eq. 55
        new = np.zeros(num_out)
        param_sum = param.sum()
        den = 0
        for d in range(num_dir):
            den += spspecial.psi(nd[d] + param_sum)
        den -= num_dir * spspecial.psi(param_sum)
        for m in range(num_out):
            for d in range(num_dir):
                new[m] += spspecial.psi(ndm[d,m] + param[m])
            new[m] -= num_dir * spspecial.psi(param[m])
            new[m] /= den
            new[m] *= param[m]
            if new[m] <= 0:
                new[m] = 1e-3/param[m].sum()
        rel_change = np.abs((new-param)).sum()/new.sum()
        if (rel_change < rtol):
            return new
        param = new
    print 'Warning: reached %d polya iterations with rtol %f > %f' % (max_iter, rel_change, rtol)
    return param

def estimate_dirichlet_newton(alpha, nlogtheta, rtol=1e-7, max_iter=30):
    '''Estimate parameters of a dirichlet distribuiton using Newton's method.'''
    M = nlogtheta.shape[0]
    nlogtheta = nlogtheta.sum(0)
    psi = spspecial.psi
    polyg = spspecial.polygamma
    for i in range(max_iter):
        old = alpha
        sumalpha = sum(alpha)
        g = -M*psi(alpha) + M*psi(sumalpha) + nlogtheta
        q = -M*polyg(1,alpha)
        qa = M*polyg(1,sumalpha)
        b = sum(g / q) / (1/qa + sum(1/q))
        alpha = alpha - (g - b) / q
        for i in [i for i, ak in enumerate(alpha) if ak <= 0.0]:
            alpha[i] = 1e-3 / alpha.sum()
        rel_change = np.abs(alpha - old).sum()/alpha.sum()
        if rel_change < rtol:
            return alpha
    print 'Warning: reached %d newton iterations with rtol %f > %f' % (max_iter, rel_change, rtol)
    return alpha

def log_multinomial_beta(a, axis=0):
    '''Log of multinomial beta function along a given axis (default 0).'''
    return spspecial.gammaln(a).sum(axis) - spspecial.gammaln(a.sum(axis))

def multinomial_beta(a, axis=0):
    '''Multinomial beta function along a given axis (default 0).'''
    return np.exp(log_multinomial_beta(a, axis))

def merge_query_stats(train, test):
    '''Merge training and test statistics.'''
    # We do not include training topics in the list so they aren't resampled
    # We don't change indices on the topics dict so test data must be first!
    stats = {
        'nmk': np.concatenate((test['nmk'], train['nmk']))
        , 'nm': np.concatenate((test['nm'], train['nm']))
        , 'nkw': train['nkw'] + test['nkw']
        , 'nk': train['nk'] + test['nk']
        , 'topics': dict(test['topics'])
    }
    return stats
    
def split_query_stats(train, combined):
    '''Get test stats from combined training-test stats after a query.'''
    num_test = combined['nmk'].shape[0] - train['nmk'].shape[0]
    stats = {
        'nmk': combined['nmk'][:num_test,:]
        , 'nm': combined['nm'][:num_test]
        , 'nkw': combined['nkw'] - train['nkw']
        , 'nk': combined['nk'] - train['nk']
        , 'topics': dict(combined['topics'])
    }
    return stats

class LdaModel(object):
    
    def __init__(self, training, num_topics, alpha=0.1, eta=0.1, burn=50, lag=4):
        '''Creates an LDA model.
        
        :param training: training corpus as (num_doc, vocab_size) array
        :param num_topics: number of topics
        :param alpha: document-topic dirichlet parameter, scalar or array,
            defaults to 0.1
        :param eta: topic-word dirichlet parameter, scalar or array,
            defaults to 0.1
        :param burn: number of "burn-in" gibbs iterations, default 50
        :param lag: number of gibbs iterations between samples, default 4
        '''
        self.num_topics = num_topics
        # Validate alpha and eta, and convert to array if necessary
        try:
            if len(alpha) != num_topics:
                raise ValueError("alpha must be a number or a num_topic-length vector")
            self.alpha = alpha
        except TypeError:
            self.alpha = np.ones(num_topics)*alpha
        try:
            if len(eta) != training.shape[1]:
                raise ValueError("eta must be a number or a vocab_size-length vector")
            self.eta = eta
        except TypeError:
            self.eta = np.ones(training.shape[1])*eta
        # Initialize gibbs sampler
        self.burn = burn
        self.lag = lag
        self.stats = self._gibbs_init(training)
        self._gibbs_sample_n(self.stats, burn)
    
    def em_iterate(self, n=1):
        '''Do n (default 1) EM iterations.'''
        for i in range(n):
            self.e_step()
            self.m_step()
    
    def e_step(self):
        '''Associate each word with a topic using Gibbs sampling.'''
        self._gibbs_sample(self.stats)
        
    def m_step(self):
        '''Update estimates for alpha and eta to maximize likelihood.'''
        self._m_alpha()
        self._m_eta()
    
    def beta(self, stats=None):
        '''Per-topic word distributions as a (num_topics, vocab_size) array.
        :param stats: Optionally specify stats, otherwise use model stats
        '''
        if stats is None:
            stats = self.stats
        result = stats['nkw'] + self.eta
        result = np.divide(result, result.sum(1)[:,np.newaxis])
        return result
    
    def theta(self, stats=None):
        '''Per-document topic distributions as a (num_docs, vocab_size) array.
        :param stats: Optionally specify stats, otherwise use model stats
        '''
        if stats is None:
            stats = self.stats
        result = stats['nmk'] + self.alpha
        result = np.divide(result, result.sum(1)[:,np.newaxis])
        return result
    
    def query(self, corpus):
        '''Find topic distributions for new documents based on trained model.
        
        :param corpus: new documents as (num_docs, vocab_size) array
        :return: topic statistics for new documents
        '''
        # Initialize the gibbs sampler for the test corpus
        test_stats = self._gibbs_init(corpus)
        # Merge training and test stats then burn in and sample
        all_stats = merge_query_stats(self.stats, test_stats)
        self._gibbs_sample_n(all_stats, self.burn + 1)
        # Split test corpus stats back out
        test_stats = split_query_stats(self.stats, all_stats)
        return test_stats

    def perplexity(self, corpus):
        '''Estimated (with Gibbs sampling) perplexity of a test corpus.'''
        # Heinrich2005 Eq. 94
        stats = self.query(corpus)
        lik = self.log_likelihood(corpus, stats)
        return np.exp(-1 * lik.sum() / stats['nm'].sum())

    def log_likelihood(self, corpus, stats):
        '''Log-likeliehood of generating a test corpus with the given corpus.'''
        # Heinrich2005 Eq. 96
        # Get per-topic word distribution for model
        beta = np.matrix(self.beta())
        # Get per-document topic distribution for test corpus
        theta = np.matrix(self.theta(stats))
        lik = np.multiply(np.log(np.array(theta*beta)), corpus).sum(1)
        return lik

    def _gibbs_init(self, corpus):
        '''Initialize Gibbs sampling by assigning a random topic to each word in
            the corpus.
        :param corpus: corpus[m][w] is the count for word w in document m
        :param skip: skip initialization for first skip docs, default 0
        :returns: statistics dict with the following keys:
            nmk: document-topic count, nmk[m][k] is for document m, topic k
            nm: document-topic sum, nm[m] is the number of words in document m
            nkw: topic-term count, nkw[k][w] is for word w in topic k
            nk: topic-term sum, nk[k] is the count of topic k in corpus
            n: total number of words
            topics: list of pairs (m, i) giving the topic for each word
        '''
        num_docs, num_words = corpus.shape
        # Initialize stats
        stats = {
            'nmk': np.zeros((num_docs, self.num_topics))
            , 'nm': np.zeros(num_docs)
            , 'nkw': np.zeros((self.num_topics, num_words))
            , 'nk': np.zeros(self.num_topics)
            , 'topics': {}
        }
        for m in xrange(num_docs):
            for i, w in enumerate(word_iter(corpus[m,:])):
                # Sample topic from uniform distribution
                k = nprand.randint(0, self.num_topics)
                stats['nmk'][m][k] += 1
                stats['nm'][m] += 1
                stats['nkw'][k][w] += 1
                stats['nk'][k] += 1
                stats['topics'][(m, i)] = (w, k)
        psi = spspecial.psi
        stats['nlogtheta'] = psi(self.alpha + stats['nmk'])
        stats['nlogtheta'] -= psi(self.alpha.sum() + stats['nm'])[:,np.newaxis]
        stats['nlogbeta'] = psi(self.eta + stats['nkw'])
        stats['nlogbeta'] -= psi(self.eta.sum() + stats['nk'])[:,np.newaxis]
        return stats
    
    def _gibbs_sample(self, stats):
        '''Resample topics for each word using Gibbs sampling, with lag.
        
        :param stats: statistics returned by _gibbs_init(), will be modified.
        '''
        self._gibbs_sample_n(stats, self.lag + 1)
    
    def _gibbs_sample_n(self, stats, n):
        '''Call _gibbs_sample_one() n times.'''
        for i in range(n):
            self._gibbs_sample_one(stats)
    
    def _gibbs_sample_one(self, stats):
        '''Resample topics for each word using Gibbs sampling, without lag.
        
        :param stats: statistics returned by _gibbs_init(), will be modified.
        '''
        # Shuffle topic assignments
        topics = stats['topics'].keys()
        nprand.shuffle(topics)
        # Resample each one
        for m, i in topics:
            # Remove doc m, word i from stats
            w, k = stats['topics'][(m, i)]
            stats['nmk'][m][k] -= 1
            stats['nm'][m] -= 1
            stats['nkw'][k][w] -= 1
            stats['nk'][k] -= 1
            # Sample from conditional
            k = sample(self.topic_conditional(m, w, stats))
            # Add new topic to stats
            stats['nmk'][m][k] += 1
            stats['nm'][m] += 1
            stats['nkw'][k][w] += 1
            stats['nk'][k] += 1
            stats['topics'][(m, i)] = (w, k)
        psi = spspecial.psi
        stats['nlogtheta'] = psi(self.alpha + stats['nmk'])
        stats['nlogtheta'] -= psi(self.alpha.sum() + stats['nm'])[:,np.newaxis]
        stats['nlogbeta'] = psi(self.eta + stats['nkw'])
        stats['nlogbeta'] -= psi(self.eta.sum() + stats['nk'])[:,np.newaxis]
    
    def _m_alpha(self):
        '''Find a new estimate for alpha that maximizes likelihood.
        
        :param iter: The number of iterations to perform, defaults to 5
        '''
        nlogtheta = self.stats['nlogtheta']
        self.alpha = estimate_dirichlet_newton(self.alpha, nlogtheta)
        
    def _m_eta(self):
        '''Find a new estimate for alpha that maximizes likelihood.
        
        :param iter: The number of iterations to perform, defaults to 5
        '''
        nlogbeta = self.stats['nlogbeta']
        self.eta = estimate_dirichlet_newton(self.eta, nlogbeta)
    
    def expected_log_likelihood(self):
        '''Expected (p(theta,beta|gibbs_z)) complete log likelihood.'''
        stats = self.stats
        psi = spspecial.psi
        num_topics = stats['nkw'].shape[0]
        num_docs = stats['nmk'].shape[0]
        # Virtual word and topic counts
        vmk = self.alpha + stats['nmk']
        vkw = self.eta + stats['nkw']
        # Calculate likelihood
        lik = 0
        lik += (stats['nlogtheta'] * (vmk - 1)).sum()
        lik += (stats['nlogbeta'] * (vkw - 1)).sum()
        lik -= num_docs * log_multinomial_beta(self.alpha)
        lik -= num_topics * log_multinomial_beta(self.eta)
        return lik
    
    def log_likelihood_wz(self):
        '''The log likelihood of the data and topic assignments.'''
        # Heinrich2005 Eq. 73
        num_docs = self.stats['nmk'].shape[0]
        lik = 0
        lik += log_multinomial_beta(self.stats['nkw'] + self.eta).sum()
        lik -= self.num_topics * log_multinomial_beta(self.eta)
        lik += log_multinomial_beta(self.stats['nmk'] + self.alpha).sum()
        lik -= num_docs * log_multinomial_beta(self.alpha)
        return lik
    
    def expected_log_likelihood_components(self):
        '''Expected (p(theta,beta|gibbs_z)) complete log likelihood.'''
        stats = self.stats
        psi = spspecial.psi
        num_topics = stats['nkw'].shape[0]
        num_docs = stats['nmk'].shape[0]
        # Virtual word and topic counts
        vmk = self.alpha + stats['nmk']
        vkw = self.eta + stats['nkw']
        # Calculate likelihood
        lik = np.array([
            (stats['nlogtheta'] * (vmk - 1)).sum()
            , (stats['nlogbeta'] * (vkw - 1)).sum()
            , num_docs * log_multinomial_beta(self.alpha)
            , num_topics * log_multinomial_beta(self.eta)
        ])
        return lik
    
    def topic_conditional(self, m, w, stats):
        '''Distribution of a single topic given others and words.
        
        :param m: index of the document to sample for
        :param w: word associated with the topic being sampled
        :param stats: count statistics (with topic being sampled removed)
        :returns: a (num_topics) length vector of topic probabilities
        '''
        pk = stats['nkw'][:,w] + self.eta[w]
        pk = np.multiply(pk, stats['nmk'][m,:] + self.alpha)
        pk = np.divide(pk, stats['nk'] + self.eta.sum())
        # Normalize
        pk /= pk.sum()
        return pk