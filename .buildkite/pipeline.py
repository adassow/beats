#!/usr/bin/env python3
from typing import Any
import yaml
import os
from dataclasses import dataclass, field
import subprocess
import fnmatch


@dataclass(unsafe_hash=True)
class Agent:
    """Buildkite Agent object"""
    image: str

    def create_entity(self):
        raise NotImplementedError("Not implemented yet")


@dataclass(unsafe_hash=True)
class AWSAgent(Agent):
    """AWS Agent object"""
    image: str

    def create_entity(self) -> dict[str, dict[str, str]]:
        return {
            "agents": {
                "provider": "aws",
                "imagePrefix": self.image,
                "instanceType": "t4g.large",
            }
        }


@dataclass(unsafe_hash=True)
class GCPAgent(Agent):
    """GCP Agent object"""
    image: str

    def create_entity(self) -> dict[str, dict[str, str]]:
        return {
            "agents": {
                "provider": "gcp",
                "image": self.image,
            }
        }


@dataclass(unsafe_hash=True)
class OrkaAgent(Agent):
    """Orka Agent object"""
    image: str

    def create_entity(self) -> dict[str, dict[str, str]]:
        return {
            "agents": {
                "provider": "orka",
                "imagePrefix": self.image,
            }
        }


@dataclass(unsafe_hash=True)
class Step:
    """Buildkite Step object"""

    command: str
    agent: Agent
    name: str
    project: str
    category: str
    label: str = field(init=False)
    comment: str = field(init=False)

    def __post_init__(self):
        self.comment = "/test " + self.project + " " + self.name
        self.label = self.name

    def __lt__(self, other):
        return self.name < other.name

    def create_entity(self) -> dict[str, Any]:
        data = {
            "label": f"{self.project} {self.name}",
            "command": [self.command],
            "notify": [{
                "github_commit_status": {
                    "context": f"{self.project}: {self.name}",
                }
            }],
        }
        data.update(self.agent.create_entity())
        return data


@dataclass(unsafe_hash=True)
class Group:
    """Buildkite Group object"""

    project: str
    category: str
    steps: list[Step]

    def __lt__(self, other):
        return self.project < other.project

    def create_entity(self) -> dict[str, Any] | None:
        if len(self.steps) == 0:
            return

        data = {
            "group": f"{self.project} {self.category}",
            "key": f"{self.project}-{self.category}",
            "steps": [step.create_entity() for step in sorted(self.steps)],
        }

        return data


@dataclass()
class Pipeline:
    """Buildkite Pipeline object"""
    groups: list[Group]

    def create_entity(self):
        data = {"steps": [group.create_entity() for group in self.groups]}
        return data


def is_pr() -> bool:
    return os.getenv('BUILDKITE_PULL_REQUEST') != "false"


def step_comment(step: Step) -> bool:
    comment = os.getenv('GITHUB_PR_TRIGGER_COMMENT')
    if comment:
        # TODO: change /test
        comment_prefix = "buildkite test " + step.project
        # i.e: /test filebeat should run all the mandatory stages
        if step.category == "mandatory" and comment_prefix == comment:
            return True
        # i.e: /test filebeat unitTest
        return comment_prefix + " " + step.name in comment
    else:
        return True


def group_comment(group: Group) -> bool:
    comment = os.getenv('GITHUB_PR_TRIGGER_COMMENT')
    if comment:
        # the comment should be a subset of the values
        # in .buildkite/pull-requests.json
        # TODO: change /test
        comment_prefix = "buildkite test"
        if group.category == "mandatory":
            # i.e: /test filebeat
            return comment_prefix + " " + group.project in comment
        else:
            # i.e: test filebeat extended
            return (
                comment_prefix + " " + group.project + " " + group.category
                in comment
            )


changed_files = None


def get_pr_changeset():
    global changed_files
    if not changed_files:
        base_branch = os.getenv('BUILDKITE_PULL_REQUEST_BASE_BRANCH')
        diff_command = [
            "git", "diff", "--name-only", "{}...HEAD".format(base_branch)
        ]
        result = subprocess.run(diff_command, stdout=subprocess.PIPE)
        changed_files = result.stdout.decode().splitlines()
        print("Changed files: {}".format(changed_files))
    return changed_files


def filter_files_by_glob(files, patterns: list[str]):
    for pattern in patterns:
        # TODO: Support glob extended patterns: ^ and etc.
        # Now it supports only linux glob patterns
        if fnmatch.filter(files, pattern):
            return True
    return False


def is_in_pr_changeset(project_changeset_filters: list[str]) -> bool:
    changeset = get_pr_changeset()
    return filter_files_by_glob(changeset, project_changeset_filters)


def is_step_enabled(step: Step) -> bool:
    if not is_pr():
        return True

    if step_comment(step):
        return True

    labels_env = os.getenv('GITHUB_PR_LABELS')
    if labels_env:
        labels = labels_env.split()
        # i.e: filebeat-unitTest
        if step.project + '-' + step.name in labels:
            return True

    return False


def is_group_enabled(group: Group, changeset_filters: list[str]) -> bool:
    if not is_pr():
        return True

    if is_pr() and is_in_pr_changeset(changeset_filters) and \
            group.category.startswith("mandatory"):
        return True

    return group_comment(group)


def fetch_stage(name: str, stage, project: str, category: str) -> Step:
    """Create a step given the yaml object."""

    agent: Agent = None
    if ("provider" not in stage) or stage["provider"] == "gcp":
        agent = GCPAgent(image=stage["platform"])
    elif stage["provider"] == "aws":
        agent = AWSAgent(image=stage["platform"])
    elif stage["provider"] == "orka":
        agent = OrkaAgent(image=stage["platform"])

    return Step(
            category=category,
            command=stage["command"],
            name=name,
            agent=agent,
            project=project)


def fetch_group(stages, project: str, category: str) -> Group:
    """Create a group given the yaml object."""

    steps = []

    for stage in stages:
        step = fetch_stage(
                category=category,
                name=stage,
                project=project,
                stage=stages[stage])

        if is_step_enabled(step):
            steps.append(step)

    return Group(
                project=project,
                category=category,
                steps=steps)


# TODO: validate unique stages!
def main() -> None:

    groups = []
    extended_groups = []
    with open(".buildkite/buildkite.yml", "r", encoding="utf8") as file:
        doc = yaml.load(file, yaml.FullLoader)

        for project in doc["projects"]:
            project_file = os.path.join(project, "buildkite.yml")
            if not os.path.isfile(project_file):
                continue
            with open(project_file, "r", encoding="utf8") as file:
                project_obj = yaml.load(file, yaml.FullLoader)

                group = fetch_group(stages=project_obj["stages"]["mandatory"],
                                    project=project,
                                    category="mandatory")

                if is_group_enabled(group, project_obj["when"]["changeset"]):
                    groups.append(group)

                group = fetch_group(stages=project_obj["stages"]["extended"],
                                    project=project,
                                    category="extended")

                if is_group_enabled(group, project_obj["when"]["changeset"]):                    
                    extended_groups.append(group)

    # TODO: improve this merging lists
    all_groups = []
    for group in sorted(groups):
        all_groups.append(group)
    for group in sorted(extended_groups):
        all_groups.append(group)

    # Produce now the pipeline
    print(yaml.dump(Pipeline(all_groups).create_entity()))


if __name__ == "__main__":

    # pylint: disable=E1120
    main()
