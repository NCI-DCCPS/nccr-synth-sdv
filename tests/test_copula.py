import numpy as np
import pytest
from copulas.multivariate import GaussianMultivariate
from jamboree_sdv.copula import LatentCopula, categorical


def test_no_fit_and_known_latent_correlation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("fit must not be called")
    monkeypatch.setattr(GaussianMultivariate, "fit", forbidden)
    model = LatentCopula(["x", "y"], [[1, .7], [.7, 1]], 42)
    draws = model.normal(30000)
    assert abs(np.corrcoef(draws.T)[0, 1] - .7) < .02


@pytest.mark.parametrize("matrix", [[[1, 2], [2, 1]], [[1, 1], [1, 1]], [[2]], [[float('nan')]]])
def test_invalid_correlation(matrix):
    with pytest.raises(ValueError):
        LatentCopula([str(i) for i in range(len(matrix))], matrix, 1)


def test_stream_invariant_to_chunk_size():
    one = LatentCopula(["x", "y"], [[1, .4], [.4, 1]], 171)
    split = LatentCopula(["x", "y"], [[1, .4], [.4, 1]], 171)
    expected = one.normal(10001)
    actual = np.concatenate([split.normal(min(137, 10001-i)) for i in range(0, 10001, 137)])
    np.testing.assert_array_equal(expected, actual)


def test_probabilities_boundaries_and_leading_zeros():
    assert categorical(np.array([0, .499, .5, np.nextafter(1., 0)]), ['001','002','003'], [1,0,1]).tolist() == ['001','001','003','003']
    with pytest.raises(ValueError):
        categorical(np.array([.1]), ['1','2'], [0,0])
    with pytest.raises(ValueError):
        categorical(np.array([.1]), ['1','2'], [1,-1])
    values = categorical(LatentCopula(['x'], [[1]], 123).uniform(20000)[:, 0], ['001','002'], [.3,.7])
    assert abs(np.mean(values == '001') - .3) < .02
