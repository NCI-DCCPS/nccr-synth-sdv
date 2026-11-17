from pathlib import Path
import json
import pytest
from jamboree_sdv.common import EXPERIMENT, WORKSPACE, output_path, read_json
from jamboree_sdv.recipe import load_recipe


def changed(tmp_path, **updates):
    data = read_json(EXPERIMENT / 'recipes/pilot-core-100-v2-replay.json')
    data.update(updates)
    path = tmp_path / 'recipe.json'
    path.write_text(json.dumps(data))
    return path


def test_pinned_recipes_and_counts():
    for n, rows in [(100, 109), (1000, 1088)]:
        for v in ['v1', 'v2']:
            r = load_recipe(EXPERIMENT / f'recipes/pilot-core-{n}-{v}-replay.json')
            assert r.targets == {'CTC': rows, 'ABM': rows, 'COG': n}


@pytest.mark.parametrize('updates', [{'unknown': 1}, {'patients': True}, {'patients': 1001}, {'seed': -1}, {'sources':['CTC','MCE']}, {'chunk_size': 0}])
def test_bad_recipes(tmp_path, updates):
    with pytest.raises(ValueError):
        load_recipe(changed(tmp_path, **updates))


def test_wrong_hash(tmp_path):
    r = read_json(EXPERIMENT / 'recipes/pilot-core-100-v1-replay.json')
    r['rules']['sha256'] = '0'*64
    with pytest.raises(ValueError, match='hash mismatch'):
        load_recipe(changed(tmp_path, rules=r['rules']))


def test_json_duplicate_and_nonfinite(tmp_path):
    path = tmp_path/'bad.json'
    for text in ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}']:
        path.write_text(text)
        with pytest.raises(ValueError): read_json(path)


def test_output_boundaries(tmp_path):
    for path in [WORKSPACE/'data/no-write', WORKSPACE/'releases/no-write', EXPERIMENT/'work/../escape']:
        with pytest.raises(ValueError): output_path(path)
    link=tmp_path/'escape';link.symlink_to(WORKSPACE/'data', target_is_directory=True)
    with pytest.raises(ValueError):output_path(link/'no-write')
    with pytest.raises(ValueError):output_path(tmp_path)

