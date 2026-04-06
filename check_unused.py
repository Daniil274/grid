import yaml
with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

used_m = set(a.get('model') for a in config.get('agents',{}).values() if 'model' in a)
for t in config.get('tools',{}).values():
    if t.get('env_vars') and 'DEFAULT_MODEL' in t.get('env_vars'):
        used_m.add(t['env_vars']['DEFAULT_MODEL'])

for c in config.get('checkers',{}).values():
    if 'model' in c: used_m.add(c['model'])
if 'embeddings' in config and 'model' in config['embeddings']:
    used_m.add(config['embeddings']['model'])

used_t = set(t for a in config.get('agents',{}).values() for t in a.get('tools',[]))

used_p = set()
for m in used_m:
    if m in config.get('models',{}):
        provider = config['models'][m].get('provider')
        if provider:
            used_p.add(provider)

all_m = set(config.get('models',{}).keys())
all_p = set(config.get('providers',{}).keys())
all_t = set(config.get('tools',{}).keys())

print('Unused Models:', all_m - used_m)
print('Unused Providers:', all_p - used_p)
print('Unused Tools:', all_t - used_t)
