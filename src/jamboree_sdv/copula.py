"""Supplied-parameter Gaussian copula; fitting is deliberately absent."""
import numpy as np
from scipy.special import ndtr
from copulas.multivariate import GaussianMultivariate


class LatentCopula:
    """A fixed internal draw block keeps the stream independent of I/O chunks."""
    def __init__(self, columns, correlation, seed):
        matrix = np.asarray(correlation, dtype=float)
        n = len(columns)
        if (not n or len(set(columns)) != n or matrix.shape != (n, n)
                or not np.isfinite(matrix).all()
                or not np.allclose(matrix, matrix.T, rtol=0, atol=1e-12)
                or not np.allclose(matrix.diagonal(), 1, rtol=0, atol=1e-12)
                or np.linalg.eigvalsh(matrix).min() <= 1e-10):
            raise ValueError("Correlation must be finite, symmetric, unit-diagonal and positive definite")
        self.model = GaussianMultivariate.from_dict({
            "type": "copulas.multivariate.gaussian.GaussianMultivariate",
            "columns": list(columns), "correlation": matrix.tolist(),
            "univariates": [{"type": "copulas.univariate.gaussian.GaussianUnivariate",
                             "loc": 0.0, "scale": 1.0} for _ in columns],
        })
        self.model.set_random_state(int(seed))
        self.columns = list(columns)
        self.buffer = np.empty((0, n))

    def normal(self, count):
        if type(count) is not int or count < 0:
            raise ValueError("count must be a nonnegative integer")
        chunks = []
        remaining = count
        while remaining:
            if len(self.buffer) == 0:
                self.buffer = self.model.sample(1024)[self.columns].to_numpy()
            take = min(remaining, len(self.buffer))
            chunks.append(self.buffer[:take])
            self.buffer = self.buffer[take:]
            remaining -= take
        return np.concatenate(chunks) if chunks else np.empty((0, len(self.columns)))

    def uniform(self, count):
        return np.clip(ndtr(self.normal(count)), 0.0, np.nextafter(1.0, 0.0))


def categorical(uniforms, categories, weights):
    weights = np.asarray(weights, dtype=float)
    u = np.asarray(uniforms, dtype=float)
    if (not categories or len(categories) != len(weights) or weights.ndim != 1
            or not np.isfinite(weights).all() or np.any(weights < 0)
            or not np.isfinite(weights.sum()) or weights.sum() <= 0
            or not np.isfinite(u).all() or np.any(u < 0) or np.any(u >= 1)):
        raise ValueError("Invalid categories, weights or uniform draws")
    cdf = np.cumsum(weights / weights.sum())
    cdf[-1] = 1.0
    return np.asarray(categories, dtype=object)[np.searchsorted(cdf, u, side="right")]

