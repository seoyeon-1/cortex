# Cortex Skill Library

스킬 = 재사용 레시피 YAML 프롬프트(+선택 Jinja2 템플릿). 태스크가 `trigger` 정규식을 만나면 프롬프트 컨텍스트로 자동 주입된다.

## 포맷 (skills/<name>.yaml)
```yaml
name: my-skill
description: one-liner
trigger: "(?i)regex that matches a task"
prompt: |
  Instructions injected into the agent context when trigger matches.
template: |
  {{ var }} - Jinja2 starter content (optional)
validate_cmd: "pytest -q"
```

## 자동 학습 플로우
1. `AgentLoop`가 패치 성공마다 패턴(도구 집합+파일 형태)을 카운트 (`.cortex_memory/skill_stats.json`).
2. 동일 패턴 3회 → `skills/candidates/skill-<hash>.yaml` 초안自动生成 + 승진 제안 출력.
3. 사람이 초안 검토(트릭거/프롬프트 수정) 후 승인:
   ```bash
   python -m core.memory.skills promote skill-<hash>   # candidates/ -> skills/ (status: active)
   python -m core.memory.skills list                    # 활성 스킬 목록
   ```
