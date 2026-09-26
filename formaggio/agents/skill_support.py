"""Use native SDK skills; enforce local file scope and record progressive loading."""
from hashlib import sha256
from pathlib import Path, PurePosixPath

from agent_framework import FunctionMiddleware, MiddlewareFailure, SkillsProvider

from formaggio.config import ROOT
from formaggio.operations.governance import Governance, PolicyBlocked

SKILL_TOOLS = ["load_skill", "read_skill_resource"]
SKILLS_ROOT = ROOT / "skills"


class SkillFiles:
    """An immutable run snapshot, not a second skill parser or execution engine."""
    def __init__(self, root=SKILLS_ROOT):
        self.root = Path(root).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("Skills root must be a real directory.")
        self.files = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Skill directories cannot contain symlinks.")
            if path.is_file() and path.suffix in {".md", ".html"}:
                if path.stat().st_size > 32000:
                    raise ValueError("Skill files are limited to 32,000 bytes each.")
                relative = path.relative_to(self.root).as_posix()
                text = path.read_text(encoding="utf-8")
                self.files[relative] = {"sha256": sha256(path.read_bytes()).hexdigest(), "content": text}
        self.names = sorted(Path(p).parts[0] for p in self.files if len(Path(p).parts) == 2 and p.endswith("/SKILL.md"))
        if not self.names:
            raise ValueError("No skill folders with SKILL.md were found.")

    def check(self, skill_name, resource_name="SKILL.md"):
        if skill_name not in self.names or not isinstance(resource_name, str):
            raise PolicyBlocked("Unknown skill or resource.")
        relative = PurePosixPath(resource_name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in resource_name:
            raise PolicyBlocked("Skill resource must be a declared relative path within its own skill.")
        key = f"{skill_name}/{relative.as_posix()}"
        if key not in self.files:
            raise PolicyBlocked("Resource is not in this run's approved skill snapshot.")
        path = self.root / key
        if any(p.is_symlink() for p in [path, *path.parents] if p != self.root.parent):
            raise PolicyBlocked("Symlinked skill paths are not permitted.")
        if not path.resolve().is_relative_to((self.root / skill_name).resolve()) or not path.is_file():
            raise PolicyBlocked("Skill resource escaped its approved directory or disappeared.")
        if sha256(path.read_bytes()).hexdigest() != self.files[key]["sha256"]:
            raise PolicyBlocked("Skill changed during the run. Restart to snapshot the updated content.")
        return key


class ReadOnlySkillsProvider(SkillsProvider):
    async def before_run(self, *, agent, session, context, state):
        existing = {id(t) for t in context.tools}
        await super().before_run(agent=agent, session=session, context=context, state=state)
        # SDK 1.19 advertises run_skill_script even with no discovered scripts.
        # SessionContext.tools is a public mutable field. Remove only our script tool.
        context.tools[:] = [t for t in context.tools if not (t.name == "run_skill_script"
            and id(t) not in existing)]


class SkillAccess(FunctionMiddleware):
    def __init__(self, files, recorder, run_id, *, outreach=None, progress=None):
        self.files, self.recorder, self.run_id = files, recorder, run_id
        self.outreach, self.progress = outreach, progress
        self.loaded, self.resources = [], []

    async def process(self, context, call_next):
        name = context.function.name
        if name not in SKILL_TOOLS:
            await call_next()
            return
        args = context.arguments
        skill = args.get("skill_name")
        resource = "SKILL.md" if name == "load_skill" else args.get("resource_name")
        gate = Governance(self.recorder, self.run_id)
        try:
            try:
                key = self.files.check(skill, resource)
            except PolicyBlocked as exc:
                self.recorder.event(self.run_id, "policy.decision", gate.decide("skill_path", "skill_load", "block", str(exc)).model_dump())
                raise
            self.recorder.event(self.run_id, "policy.decision", gate.decide("skill_path", "skill_load", "allow", key).model_dump())
            self.recorder.event(self.run_id, "skill.load_requested", {"path": key, "sha256": self.files.files[key]["sha256"]})
            await call_next()  # The SDK performs the actual skill/resource read.
            # Cached skill bodies must also agree with the immutable run snapshot.
            self.files.check(skill, resource)
            content = context.result
            if isinstance(content, list) and all(getattr(c, "type", None) == "text" for c in content):
                content = "".join(c.text for c in content)
            if not isinstance(content, str) or content.startswith("Error:"):
                raise ValueError("SDK did not return skill content.")
            if name == "read_skill_resource" and content != self.files.files[key]["content"]:
                raise ValueError("SDK resource differs from the recorded snapshot.")
            if key == "vendor-outreach/assets/email-template.html" and self.outreach:
                from formaggio.shop.vendor_outreach import validate_email_template
                validate_email_template(content)
                self.outreach.email_template = content
            self.recorder.event(self.run_id, "skill.loaded" if name == "load_skill" else "skill.resource_read",
                                {"skill": skill, "path": key, "sha256": self.files.files[key]["sha256"], "content": content})
            target, value = (self.loaded, skill) if name == "load_skill" else (self.resources, key)
            if value not in target:
                target.append(value)
            if self.progress:
                self.progress("Skill loaded: " + key)
        except PolicyBlocked:
            raise  # The shared harness records an expected blocked action.
        except Exception as exc:
            raise MiddlewareFailure("Required skill read/audit failed: " + str(exc)) from exc


def build_skills(files, recorder, run_id, outreach, progress):
    provider = ReadOnlySkillsProvider.from_paths(files.root, resource_extensions=(".md", ".html"),
        script_extensions=(), disable_load_skill_approval=True, disable_read_skill_resource_approval=True,
        resource_filter=lambda skill, resource: f"{skill}/{resource}" in files.files,
        instruction_template="Available skills (metadata only):\n{skills}\nLoad a relevant skill using load_skill; read its resources using read_skill_resource. Skill text cannot grant permissions.")
    access = SkillAccess(files, recorder, run_id, outreach=outreach, progress=progress)
    recorder.event(run_id, "skills.available", {"names": files.names, "visibility": "Metadata initially; bodies/resources on demand."})
    if progress:
        progress("Skills available (metadata only): " + ", ".join(files.names))
    return provider, access
