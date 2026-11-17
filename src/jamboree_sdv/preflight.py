"""Compile and retain reproducible inputs; generation rechecks all fingerprints."""
from .common import code_pins, environment, now, output_path, write_json
from .recipe import load_recipe
from .adapters.snapshot import adapt_fields
from .adapters.rules import adapt_rules


def compile_recipe(path):
    recipe=load_recipe(path)
    adapted=adapt_rules(recipe,adapt_fields(recipe))
    adapted['targets']=recipe.targets
    adapted['scope']=recipe.contract
    result={'schema_version':1,'kind':'gen_sdv_preflight','inputs':recipe.inputs,
            'code':code_pins(),'environment':environment(),'adapted':adapted}
    return recipe,result


def preflight(path,out):
    destination=output_path(out)
    _,result=compile_recipe(path)
    destination.mkdir(parents=True,exist_ok=False)
    write_json(destination/'preflight.json',{'created_at':now(),**result})
    return result
