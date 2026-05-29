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
DEFAULT_STATE_FILENAME = ".agent_state.json"


@dataclass
class WorkspaceFiles:
    """工作空间文件路径"""
    agent_path: str
    state_path: str


def ensure_workspace(workspace_dir: str, create_templates: bool = True) -> WorkspaceFiles:
    """确保工作空间存在，并同步 AGENT.md 模板"""
    os.makedirs(workspace_dir, exist_ok=True)

    agent_path = os.path.join(workspace_dir, DEFAULT_AGENT_FILENAME)
    state_path = os.path.join(workspace_dir, DEFAULT_STATE_FILENAME)

    skills_dir = os.path.join(workspace_dir, "skills")
    os.makedirs(skills_dir, exist_ok=True)

    if create_templates:
        _sync_template(agent_path, _get_agent_template())
        logger.debug(f"[Workspace] Initialized workspace at: {workspace_dir}")

    return WorkspaceFiles(agent_path=agent_path, state_path=state_path)


def overwrite_workspace_prompts(workspace_dir: str) -> WorkspaceFiles:
    """Reset AGENT.md from repo template (called on user reset command)."""
    workspace_files = ensure_workspace(workspace_dir, create_templates=True)
    try:
        with open(workspace_files.agent_path, "w", encoding="utf-8") as f:
            f.write(_get_agent_template())
        logger.info(f"[Workspace] Reset prompt template: {workspace_files.agent_path}")
    except Exception as e:
        logger.warning(f"[Workspace] Failed to reset prompt template: {e}")
    return workspace_files


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
        files_to_load = [DEFAULT_AGENT_FILENAME]
    
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


def _sync_template(filepath: str, template_content: str):
    """每次启动都从 templates/ 同步，保证模板改动立即生效（不保留旧内容）"""
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(template_content)
        logger.debug(f"[Workspace] Synced template: {os.path.basename(filepath)}")
    except Exception as e:
        logger.error(f"[Workspace] Failed to sync template {filepath}: {e}")


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

