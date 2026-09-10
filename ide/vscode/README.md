# cortex-vscode (scaffold)

Python 측 LSP(`python -m core.lsp.server`)와 대시보드를 붙여 쓰는 VS Code 클라이언트 스캐폴드.

```bash
npm install && npm run compile      # → out/extension.js
code .                              # F5: Extension Development Host 실행
```

env `CORTEX_HOME` = cortex/ 리포 경로. 기능:
- `Cortex: Run Task on This Repo` — 입력창 → 터미널에서 `main.py --yes` 실행
- `Cortex: Open Live Dashboard` — 대시보드 프로세스 기동 + 브라우저 열기
- 커서 위치 파일의 심볼 네비게이션(tree-sitter 그래프 기반 documentSymbol/workspace/symbol)

Diff 미리보기 사이드바·breakpoint 연동은 Phase 6(에이전트 마디별 stop 지점 이벤트가 이미 `/api/stream`에 있음) 확장 예정.
