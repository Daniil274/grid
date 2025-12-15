"""
APE (Automatic Prompt Engineer) tool for Grid Agents.
Wraps the automatic_prompt_engineer library to provide prompt optimization capabilities.
"""
import os
import re
import sys
from typing import Tuple, List, Optional
from agents import function_tool, RunContextWrapper
from core.config import Config

# Ensure automatic_prompt_engineer is in path
current_dir = os.path.dirname(os.path.abspath(__file__))
ape_dir = os.path.join(current_dir, 'automatic_prompt_engineer')
if ape_dir not in sys.path:
    sys.path.append(ape_dir)

try:
    from automatic_prompt_engineer import ape, config as ape_config
except ImportError:
    # Fallback if the path structure is slightly different in installed package vs local
    from .automatic_prompt_engineer.automatic_prompt_engineer import ape
    from .automatic_prompt_engineer.automatic_prompt_engineer import config as ape_config

@function_tool
def run_ape_tool(
    context: RunContextWrapper,
    task: str
) -> str:
    """
    Инструмент для генерации системного промпта для агента под конкретную задачу.
    Использует возможности LLM для создания оптимизированной инструкции (System Prompt).
    
    Args:
        task: Описание задачи, которую должен выполнять агент.
              Пример: "Написать веб-сервер на FastAPI с базой данных PostgreSQL".
                      
    Returns:
        Рекомендованный системный промпт для агента.
    """
    
    # 1. Config from Grid
    try:
        grid_config = Config()
        model_name = "gpt-5-nano" # Target model from requirements
        
        # Resolve model details
        model_cfg = grid_config.get_model(model_name)
        provider_key = model_cfg.provider
        provider_cfg = grid_config.get_provider(provider_key)
        
        api_key = grid_config.get_api_key(provider_key)
        base_url = provider_cfg.base_url
        
        if not api_key:
             return f"Ошибка: API Key для провайдера '{provider_key}' не найден в конфигурации."

    except Exception as e:
        return f"Ошибка конфигурации Grid: {str(e)}"

    
    # 2. Run Generation using APE infrastructure
    try:
        # We use APE's generate module to produce candidates
        from automatic_prompt_engineer import generate, template, config as ape_config

        # Configure manually
        conf = {
            'generation': {
                'num_subsamples': 1,
                'num_demos': 1, # Must be <= len(dummy_data) which is 1
                'num_prompts_per_subsample': 5, # Generate 5 candidates
                'model': {
                    'name': 'GPT_forward',
                    'batch_size': 5,
                    'gpt_config': {
                        'model': model_name,
                        'temperature': 0.7,
                        'max_tokens': 2000,
                        'top_p': 1.0,
                        'frequency_penalty': 0.0,
                        'presence_penalty': 0.0
                    },
                    # Inject credentials
                    'api_key': api_key,
                    'base_url': base_url
                }
            }
        }
        
        # Meta-prompt for generating system prompts
        meta_template = (
            f"You are an expert Prompt Engineer. Your goal is to write a high-quality System Prompt for an autonomous AI agent.\n"
            f"The agent will have access to tools: Filesystem, Git, Terminal.\n"
            f"The agent's specific task is: {task}\n\n"
            f"Write a detailed, structured System Prompt that instructs the agent how to think, behave, and solve this task effectively.\n"
            f"System Prompt:\n[APE]"
        )
        
        # We don't use data-driven induction here (no input/output pairs), just generation based on task description.
        # Passing dummy data as generate_prompts expects some structure, but we won't use [INPUT]/[OUTPUT] in template.
        dummy_data = ([""], [""]) 
        demos_template = template.DemosTemplate("Input: [INPUT]\nOutput: [OUTPUT]") # Not used but required by signature
        
        prompts = generate.generate_prompts(
            prompt_gen_template=template.GenerationTemplate(meta_template),
            demos_template=demos_template,
            prompt_gen_data=dummy_data,
            config=conf['generation']
        )
        
        # 3. Simple selection
        # User requested "one best". 
        # Since we don't have evaluation data, we use a heuristic: 
        # The longest prompt is likely the most detailed and comprehensive.
        
        if not prompts:
            return "Не удалось сгенерировать промпт."

        best_prompt = max(prompts, key=len).strip()
        
        return best_prompt
        
    except Exception as e:
        return f"Ошибка при выполнении APE: {str(e)}"

APE_TOOLS = {
    "automatic_prompt_engineer": run_ape_tool,
}
