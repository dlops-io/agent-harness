"""Small explicit feature blocks for the classroom prompt files."""
import re


def render_prompt(template, features):
    """Optional instructions use <!-- if:feature --> ... <!-- endif -->."""
    pattern = r"<!-- if:(planning|skills) -->(.*?)<!-- endif -->"
    prompt = re.sub(pattern, lambda match: match[2] if match[1] in features else "", template, flags=re.S)
    if "<!-- if:" in prompt or "<!-- endif -->" in prompt:
        raise ValueError("Unbalanced or unknown prompt feature block.")
    return prompt
