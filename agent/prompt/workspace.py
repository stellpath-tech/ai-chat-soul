"""
Workspace Management - 工作空间管理模块

负责初始化工作空间、创建模板文件、加载上下文文件
"""

from __future__ import annotations
import os
import json
from typing import List, Optional, Dict
from dataclasses import dataclass

from common.log import logger
from .builder import ContextFile


# 默认文件名常量
DEFAULT_AGENT_FILENAME = "AGENT.md"
DEFAULT_USER_FILENAME = "USER.md"
DEFAULT_RULE_FILENAME = "RULE.md"
DEFAULT_MEMORY_FILENAME = "MEMORY.md"
DEFAULT_STATE_FILENAME = ".agent_state.json"


@dataclass
class WorkspaceFiles:
    """工作空间文件路径"""
    agent_path: str
    user_path: str
    rule_path: str
    memory_path: str
    memory_dir: str
    state_path: str


def ensure_workspace(workspace_dir: str, create_templates: bool = True) -> WorkspaceFiles:
    """
    确保工作空间存在，并创建必要的模板文件
    
    Args:
        workspace_dir: 工作空间目录路径
        create_templates: 是否创建模板文件（首次运行时）
        
    Returns:
        WorkspaceFiles对象，包含所有文件路径
    """
    # 确保目录存在
    os.makedirs(workspace_dir, exist_ok=True)
    
    # 定义文件路径
    agent_path = os.path.join(workspace_dir, DEFAULT_AGENT_FILENAME)
    user_path = os.path.join(workspace_dir, DEFAULT_USER_FILENAME)
    rule_path = os.path.join(workspace_dir, DEFAULT_RULE_FILENAME)
    memory_path = os.path.join(workspace_dir, DEFAULT_MEMORY_FILENAME)  # MEMORY.md 在根目录
    memory_dir = os.path.join(workspace_dir, "memory")  # 每日记忆子目录
    state_path = os.path.join(workspace_dir, DEFAULT_STATE_FILENAME)  # 状态文件
    
    # 创建memory子目录
    os.makedirs(memory_dir, exist_ok=True)

    # 创建skills子目录 (for workspace-level skills installed by agent)
    skills_dir = os.path.join(workspace_dir, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    
    # 如果需要，创建模板文件
    if create_templates:
        _create_template_if_missing(agent_path, _get_agent_template())
        _create_template_if_missing(user_path, _get_user_template())
        _create_template_if_missing(rule_path, _get_rule_template())
        _create_template_if_missing(memory_path, _get_memory_template())
        
        logger.debug(f"[Workspace] Initialized workspace at: {workspace_dir}")
    
    return WorkspaceFiles(
        agent_path=agent_path,
        user_path=user_path,
        rule_path=rule_path,
        memory_path=memory_path,
        memory_dir=memory_dir,
        state_path=state_path
    )


def load_context_files(workspace_dir: str, files_to_load: Optional[List[str]] = None) -> List[ContextFile]:
    """
    加载工作空间的上下文文件
    
    Args:
        workspace_dir: 工作空间目录
        files_to_load: 要加载的文件列表（相对路径），如果为None则加载所有标准文件
        
    Returns:
        ContextFile对象列表
    """
    if files_to_load is None:
        # 默认加载的文件（按优先级排序）
        files_to_load = [
            DEFAULT_AGENT_FILENAME,
            DEFAULT_USER_FILENAME,
            DEFAULT_RULE_FILENAME,
        ]
    
    context_files = []
    
    for filename in files_to_load:
        filepath = os.path.join(workspace_dir, filename)
        
        if not os.path.exists(filepath):
            continue
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            # 跳过空文件或只包含模板占位符的文件
            if not content or _is_template_placeholder(content):
                continue
            
            context_files.append(ContextFile(
                path=filename,
                content=content
            ))
            
            logger.debug(f"[Workspace] Loaded context file: {filename}")
            
        except Exception as e:
            logger.warning(f"[Workspace] Failed to load {filename}: {e}")
    
    return context_files


def _create_template_if_missing(filepath: str, template_content: str):
    """如果文件不存在，创建模板文件"""
    if not os.path.exists(filepath):
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(template_content)
            logger.debug(f"[Workspace] Created template: {os.path.basename(filepath)}")
        except Exception as e:
            logger.error(f"[Workspace] Failed to create template {filepath}: {e}")


def _is_template_placeholder(content: str) -> bool:
    """检查内容是否为模板占位符"""
    # 常见的占位符模式
    placeholders = [
        "*(填写",
        "*(在首次对话时填写",
        "*(可选)",
        "*(根据需要添加",
    ]
    
    lines = content.split('\n')
    non_empty_lines = [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
    
    # 如果没有实际内容（只有标题和占位符）
    if len(non_empty_lines) <= 3:
        for placeholder in placeholders:
            if any(placeholder in line for line in non_empty_lines):
                return True
    
    return False


# ============= 模板内容 =============

def _get_templates_dir() -> str:
    """获取模板目录路径（项目根目录下的 templates/）"""
    # workspace.py 位于 agent/prompt/，项目根目录在两级之上
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(current_dir))
    return os.path.join(project_root, "templates")


def _read_template_file(filename: str, fallback: str = "") -> str:
    """从 templates/ 目录读取模板文件，读取失败则返回 fallback"""
    filepath = os.path.join(_get_templates_dir(), filename)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.warning(f"[Workspace] Failed to read template {filepath}: {e}")
        return fallback


def _get_agent_template() -> str:
    """Agent人格设定模板 - 从 templates/AGENT.md 读取"""
    return _read_template_file("AGENT.md", fallback="# AGENT.md\n")


def _get_user_template() -> str:
    """用户身份信息模板 - 从 templates/USER.md 读取"""
    return _read_template_file("USER.md", fallback="# USER.md\n")


def _get_rule_template() -> str:
    """工作空间规则模板 - 从 templates/RULE.md 读取"""
    return _read_template_file("RULE.md", fallback="# RULE.md\n")


def _get_memory_template() -> str:
    """长期记忆模板 - 创建一个空文件，由 Agent 自己填充"""
    return """# MEMORY.md - 长期记忆

*这是你的长期记忆文件。记录重要的事件、决策、偏好、学到的教训。*

---

"""


# ============= 状态管理 =============

def is_first_conversation(workspace_dir: str) -> bool:
    """
    判断是否为首次对话
    
    Args:
        workspace_dir: 工作空间目录
        
    Returns:
        True 如果是首次对话，False 否则
    """
    state_path = os.path.join(workspace_dir, DEFAULT_STATE_FILENAME)
    
    if not os.path.exists(state_path):
        return True
    
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        return not state.get('has_conversation', False)
    except Exception as e:
        logger.warning(f"[Workspace] Failed to read state file: {e}")
        return True


def mark_conversation_started(workspace_dir: str):
    """
    标记已经发生过对话
    
    Args:
        workspace_dir: 工作空间目录
    """
    state_path = os.path.join(workspace_dir, DEFAULT_STATE_FILENAME)
    
    state = {
        'has_conversation': True,
        'first_conversation_time': None
    }
    
    # 如果文件已存在，保留原有的首次对话时间
    if os.path.exists(state_path):
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                old_state = json.load(f)
            if 'first_conversation_time' in old_state:
                state['first_conversation_time'] = old_state['first_conversation_time']
        except Exception as e:
            logger.warning(f"[Workspace] Failed to read old state: {e}")
    
    # 如果是首次标记，记录时间
    if state['first_conversation_time'] is None:
        from datetime import datetime
        state['first_conversation_time'] = datetime.now().isoformat()
    
    try:
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        logger.info(f"[Workspace] Marked conversation as started")
    except Exception as e:
        logger.error(f"[Workspace] Failed to write state file: {e}")

