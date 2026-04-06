import sys
import ruamel.yaml

def main():
    yaml = ruamel.yaml.YAML()
    yaml.preserve_quotes = True
    
    with open('config.yaml', 'r', encoding='utf-8') as f:
        data = yaml.load(f)

    # Calculate used elements recursively
    used_models = set()
    used_tools = set()

    # 1. Models from agents
    if 'agents' in data:
        for agent_name, agent_data in data['agents'].items():
            if 'model' in agent_data:
                used_models.add(agent_data['model'])
            if 'tools' in agent_data:
                for tool in agent_data['tools']:
                    used_tools.add(tool)

    # 2. Models from checkers
    if 'checkers' in data:
        for checker in data['checkers'].values():
            if 'model' in checker:
                used_models.add(checker['model'])

    # 3. Model from embeddings
    if 'embeddings' in data and 'model' in data['embeddings']:
        used_models.add(data['embeddings']['model'])

    # 4. DEFAULT_MODEL from tools
    if 'tools' in data:
        for t_name, t_data in data['tools'].items():
            if isinstance(t_data, dict) and 'env_vars' in t_data and 'DEFAULT_MODEL' in t_data['env_vars']:
                used_models.add(t_data['env_vars']['DEFAULT_MODEL'])

    # Remove unused tools
    if 'tools' in data:
        tools_to_remove = [k for k in data['tools'].keys() if k not in used_tools]
        # Make sure coordinator is removed
        if 'coordinator' in data['tools']:
            tools_to_remove.append('coordinator')
        for t in tools_to_remove:
            if t in data['tools']:
                del data['tools'][t]

    # Remove unused models
    if 'models' in data:
        models_to_remove = [k for k in data['models'].keys() if k not in used_models]
        for m in models_to_remove:
            del data['models'][m]

    # Calculate used providers
    used_providers = set()
    if 'models' in data:
        for m_name, m_data in data['models'].items():
            if 'provider' in m_data:
                used_providers.add(m_data['provider'])

    # Remove unused providers
    if 'providers' in data:
        providers_to_remove = [k for k in data['providers'].keys() if k not in used_providers]
        for p in providers_to_remove:
            del data['providers'][p]

    # Clean up coordinator_prompt
    if 'prompt_templates' in data and 'coordinator_prompt' in data['prompt_templates']:
        del data['prompt_templates']['coordinator_prompt']

    with open('config.yaml', 'w', encoding='utf-8') as f:
        yaml.dump(data, f)

    print("Config cleaned successfully!")

if __name__ == '__main__':
    main()
