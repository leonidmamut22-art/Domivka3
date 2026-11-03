"""Validate the repository blueprint against Render's current public schema."""
import json
from pathlib import Path
from urllib.request import urlopen
import jsonschema
import yaml

root = Path(__file__).resolve().parents[3]
with urlopen('https://render.com/schema/render.yaml.json', timeout=30) as response:
    schema = json.load(response)
blueprint = yaml.safe_load((root / 'render.yaml').read_text(encoding='utf-8'))
jsonschema.Draft202012Validator(schema).validate(blueprint)
assert all(service.get('plan') == 'free' for service in blueprint['services'])
assert 'databases' not in blueprint, 'Use external persistent Neon, not expiring Render Free Postgres'
print('Render blueprint schema valid; free web service, no expiring database')
