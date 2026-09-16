import os
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame, QHBoxLayout,
    QGridLayout
)
from aether_ui.theme import (
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, SURFACE_BG, SURFACE_PANEL, SURFACE_BORDER
)


class SkillsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.skills_dir = Path.home() / ".aether" / "skills"
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        # Header
        header = QLabel("Skills")
        header.setStyleSheet(f"font-size: 24px; font-weight: 700; color: {TEXT_PRIMARY};")
        main_layout.addWidget(header)

        desc = QLabel("Skills are specialized workflows and knowledge distilled by Aether.\nThey exist as SKILL.md files inside your ~/.aether/skills directory.")
        desc.setStyleSheet(f"font-size: 14px; color: {TEXT_SECONDARY};")
        desc.setWordWrap(True)
        main_layout.addWidget(desc)

        # Scroll Area for Skills
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: transparent; }}")
        
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(12)
        
        scroll.setWidget(self.scroll_content)
        main_layout.addWidget(scroll)

        self._refresh_skills()

    def _refresh_skills(self):
        # Clear existing
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        skills_found = []
        if self.skills_dir.exists():
            for entry in self.skills_dir.iterdir():
                if entry.is_dir() and (entry / "SKILL.md").exists():
                    skills_found.append(entry.name)
                elif entry.is_file() and entry.name.endswith(".md"):
                    skills_found.append(entry.stem)
                    
        if not skills_found:
            self._show_empty_state()
        else:
            for skill in sorted(skills_found):
                self._add_skill_card(skill)
            
        self.scroll_layout.addStretch()

    def _show_empty_state(self):
        empty_card = QFrame()
        empty_card.setStyleSheet(f"""
            QFrame {{
                background-color: {SURFACE_PANEL};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 8px;
            }}
        """)
        empty_layout = QVBoxLayout(empty_card)
        empty_layout.setContentsMargins(30, 40, 30, 40)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        title = QLabel("No Skills Found")
        title.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT_PRIMARY};")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(title)
        
        msg = QLabel("Aether hasn't learned any custom skills yet. In the future (Phase 8), Aether will distill completed tasks into reusable skills here.\n\nTo manually add a skill, place a SKILL.md file in:\n~/.aether/skills/")
        msg.setStyleSheet(f"font-size: 13px; color: {TEXT_SECONDARY};")
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(msg)
        
        self.scroll_layout.addWidget(empty_card)

    def _add_skill_card(self, name: str):
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {SURFACE_PANEL};
                border: 1px solid {SURFACE_BORDER};
                border-radius: 8px;
            }}
        """)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {TEXT_PRIMARY};")
        layout.addWidget(name_lbl)
        
        layout.addStretch()
        
        type_lbl = QLabel("Markdown Skill")
        type_lbl.setStyleSheet(f"font-size: 12px; color: {TEXT_MUTED};")
        layout.addWidget(type_lbl)
        
        self.scroll_layout.addWidget(card)
