import argparse
import os
import sys
import yaml
import subprocess
from rich.console import Console
from rich.traceback import install
from rich.panel import Panel
from core.orchestrator import Orchestrator  # Phase 6: formerly core.loop.AgentLoop
from core.sandbox import Sandbox

install(show_locals=True)
console = Console()

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def find_repo_root(path: str) -> str:
    p = os.path.abspath(path)
    while p != os.path.dirname(p):
        if os.path.exists(os.path.join(p, ".git")):
            return p
        p = os.path.dirname(p)
    return os.getcwd()

def main():
    parser = argparse.ArgumentParser(description="Cortex - Autonomous Coding Agent v1.0")
    parser.add_argument("task", type=str, nargs="?", help="Task description (e.g., 'Fix failing tests in test_math.py')")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--repo", default=".", help="Target repository path (must be git repo)")
    parser.add_argument("--dry-run", action="store_true", help="Run in read-only mode (no sandbox write)")
    parser.add_argument("--yes", "-y", action="store_true", help="Auto-apply the generated patch (non-interactive; for GitHub App / CI)")
    parser.add_argument("--resume", nargs="?", const="", default=None, metavar="TASK_ID",
                        help="Durable resume: replay completed steps and continue (optionally by task id)")
    args = parser.parse_args()

    if not args.task:
        parser.print_help()
        console.print("\n[bold red]Error: Task description is required.[/bold red]")
        sys.exit(1)

    if not os.path.exists(args.config):
        console.print(f"[bold red]Config file not found: {args.config}[/bold red]")
        sys.exit(1)

    config = load_config(args.config)
    repo_root = find_repo_root(args.repo)

    console.print(Panel(f"[bold]CORTEX v1.0[/bold]\nRepo: {repo_root}\nTask: {args.task}", title="Initialization", border_style="blue"))

    sandbox = Sandbox(repo_root)

    with sandbox.session() as sandbox_path:
        console.print(f"[green]Sandbox active at:[/green] {sandbox_path}")

        agent = Orchestrator(sandbox_path, config)
        success = agent.run(args.task, resume=bool(args.resume is not None),
                            task_id=(args.resume or None))

        if success:
            console.rule("[bold green]TASK COMPLETED IN SANDBOX")
            diff = sandbox.get_diff()
            if diff.strip():
                console.print("[bold]Generated Patch:[/bold]")
                console.print(diff)

                if not args.dry_run:
                    auto = getattr(args, "yes", False)
                    if auto or console.input("[yellow]Apply this patch to main repository? (y/N): [/yellow]").lower() == 'y':
                        try:
                            import tempfile
                            with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False, encoding='utf-8') as tf:
                                # GitPython strips the trailing newline off `git diff` output;
                                # git apply requires it (corrupt-patch EOF otherwise).
                                tf.write(diff if diff.endswith("\n") else diff + "\n")
                                patch_file = tf.name

                            result = subprocess.run(["git", "apply", patch_file], cwd=repo_root, capture_output=True, text=True)
                            os.unlink(patch_file)

                            if result.returncode == 0:
                                console.print("[bold green]Patch applied successfully to main repo![/bold green]")
                            else:
                                console.print(f"[bold red]Failed to apply patch:[/bold red]\n{result.stderr}")
                        except Exception as e:
                            console.print(f"[bold red]Apply error: {e}[/bold red]")
                    else:
                        console.print("[yellow]Patch not applied. Saved in sandbox history.[/yellow]")
            else:
                console.print("[yellow]No changes detected.[/yellow]")
        else:
            console.rule("[bold red]TASK FAILED")
            console.print("Sandbox will be discarded automatically.")

if __name__ == "__main__":
    main()
