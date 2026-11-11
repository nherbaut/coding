"""Fixed-host GitHub App HTTP client. Never include remote bodies/tokens in errors."""

import base64
import json
import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import jwt
from django.conf import settings


class GitHubError(ValueError):
    def __init__(self, message, *, retry_after=60, conflict=False, status=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.conflict = conflict
        self.status = status


def configured():
    return all(
        (
            settings.GITHUB_APP_ID,
            settings.GITHUB_CLIENT_ID,
            settings.GITHUB_CLIENT_SECRET,
            settings.GITHUB_PRIVATE_KEY_FILE,
            settings.GITHUB_APP_SLUG,
        )
    )


def http(url, token="", method="GET", data=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "api.github.com",
        "github.com",
    }:
        raise GitHubError("Invalid GitHub endpoint")
    headers = {
        "Accept": (
            "application/json"
            if parsed.netloc == "github.com"
            else "application/vnd.github+json"
        ),
        "User-Agent": "Corpus-Coding",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        headers=headers,
        method=method,
        data=json.dumps(data).encode() if data is not None else None,
    )

    # Redirects must not forward credentials to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=30
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        reset = error.headers.get("X-RateLimit-Reset", "0")
        delay = max(
            (
                int(error.headers.get("Retry-After", "60"))
                if error.headers.get("Retry-After", "60").isdigit()
                else 60
            ),
            int(reset) - int(time.time()) if reset.isdigit() else 60,
        )
        messages = {
            401: "GitHub authorization expired or was revoked",
            403: "GitHub denied access, branch rules or API rate limits",
            404: "GitHub repository, branch or installation is unavailable",
            409: "GitHub branch changed concurrently",
            422: "GitHub rejected the update: concurrent changes or branch rules",
        }
        raise GitHubError(
            messages.get(error.code, f"GitHub request failed (HTTP {error.code})"),
            retry_after=min(max(delay, 30), 86400),
            conflict=error.code in (409, 422),
            status=error.code,
        ) from None
    except (OSError, ValueError):
        raise GitHubError(
            "GitHub is temporarily unavailable; the operation can be retried"
        ) from None


class GitHubClient:
    def __init__(self, token):
        self.token = token

    def api(self, path, method="GET", data=None):
        return http("https://api.github.com" + path, self.token, method, data)

    def pages(self, path, key=None):
        result = []
        for page in range(1, 1001):
            sep = "&" if "?" in path else "?"
            response = self.api(f"{path}{sep}per_page=100&page={page}")
            items = response[key] if key else response
            result.extend(items)
            if len(items) < 100:
                return result
        raise GitHubError("GitHub result exceeds the supported pagination limit")

    def repositories(self):
        result = []
        for installation in self.pages("/user/installations", "installations"):
            for repo in self.pages(
                f"/user/installations/{installation['id']}/repositories", "repositories"
            ):
                if repo.get("permissions", {}).get("push"):
                    result.append(
                        dict(
                            id=repo["id"],
                            name=repo["full_name"],
                            installation=installation["id"],
                            private=repo["private"],
                        )
                    )
        return result

    def repository_access(self, repository_id, installation):
        repo = self.api(f"/repositories/{repository_id}")
        if not repo.get("permissions", {}).get("push"):
            raise GitHubError(
                "Your GitHub account must have write access to this repository"
            )
        matches = self.pages(
            f"/user/installations/{installation}/repositories", "repositories"
        )
        if not any(item["id"] == repository_id for item in matches):
            raise GitHubError(
                "This repository is not available through your installation"
            )
        return repo

    def head(self, repo, branch):
        ref = self.api(
            f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(branch,safe='')}"
        )
        commit = self.api(f"/repos/{repo}/git/commits/{ref['object']['sha']}")
        return dict(sha=ref["object"]["sha"], tree=commit["tree"]["sha"])

    def initial_head(self, repo, branch):
        """Only recognize an actually branchless repository, not a missing ref."""
        try:
            return self.head(repo, branch)
        except GitHubError as error:
            if error.status not in (404, 409):
                raise
            branches = self.api(f"/repos/{repo}/branches?per_page=1")
            if branches:
                raise error
            repository = self.api(f"/repos/{repo}")
            if branch != repository["default_branch"]:
                raise GitHubError(
                    "For an empty repository, choose its default branch: "
                    + repository["default_branch"]
                )
            return dict(sha="", tree="")

    def seed_repository(self, repo, branch, path, content, message):
        # The Contents API initializes the default branch of a branchless repo.
        repository = self.api(f"/repos/{repo}")
        if repository["default_branch"] != branch:
            raise GitHubError(
                "The empty repository default branch changed; retry with its current default branch",
                conflict=True,
            )
        # No sha: an existing file can never be overwritten.
        result = self.api(
            f"/repos/{repo}/contents/{urllib.parse.quote(path,safe='/')}",
            "PUT",
            dict(
                message=message,
                content=base64.b64encode(content.encode("utf-8")).decode("ascii"),
            ),
        )
        return result["commit"]["sha"]

    def tree(self, repo, tree):
        result = {}

        def walk(sha, prefix=""):
            response = self.api(f"/repos/{repo}/git/trees/{sha}")
            if response.get("truncated"):
                raise GitHubError("GitHub returned an incomplete repository tree")
            for item in response["tree"]:
                path = prefix + item["path"]
                if item["type"] == "tree":
                    walk(item["sha"], path + "/")
                else:
                    result[path] = item
                if len(result) > 100000:
                    raise GitHubError("Repository exceeds 100000 files")

        # Only traverse the two data roots. Other repository content is preserved.
        response = self.api(f"/repos/{repo}/git/trees/{tree}")
        if response.get("truncated"):
            raise GitHubError("GitHub returned an incomplete repository tree")
        for item in response["tree"]:
            if item["path"] in ("corpus", "codebook"):
                if item["type"] != "tree":
                    raise GitHubError("corpus and codebook must be directories")
                prefix = item["path"] + "/"
                subtree = self.api(f"/repos/{repo}/git/trees/{item['sha']}?recursive=1")
                if subtree.get("truncated"):
                    walk(item["sha"], prefix)
                else:
                    for child in subtree["tree"]:
                        if child["type"] != "tree":
                            result[prefix + child["path"]] = child
                    if len(result) > 100000:
                        raise GitHubError("Repository exceeds 100000 files")
        return result

    def read_files(self, repo, tree):
        result, total, actual = {}, 0, 0
        for path, item in sorted(tree.items()):
            if item.get("mode") not in ("100644", "100755") or item["type"] != "blob":
                raise GitHubError(f"Symlinks and submodules are not supported: {path}")
            total += item.get("size", 0)
            if total > settings.GITHUB_IMPORT_MAX_BYTES:
                raise GitHubError("Repository data exceeds the 100 MB import limit")
            blob = self.api(f"/repos/{repo}/git/blobs/{item['sha']}")
            if blob.get("encoding") != "base64":
                raise GitHubError(f"Unsupported GitHub blob encoding: {path}")
            raw = base64.b64decode(blob["content"])
            if (
                hashlib.sha1(
                    b"blob " + str(len(raw)).encode() + b"\0" + raw
                ).hexdigest()
                != item["sha"]
            ):
                raise GitHubError(f"GitHub blob hash differs: {path}")
            actual += len(raw)
            if actual > settings.GITHUB_IMPORT_MAX_BYTES:
                raise GitHubError("Repository data exceeds the 100 MB import limit")
            try:
                result[path] = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise GitHubError(f"File is not UTF-8 text: {path}") from None
        return result

    def commit_files(self, repo, branch, head, files, message, heartbeat=lambda: None):
        entries = []
        for path, content in sorted(files.items()):
            heartbeat()
            blob = self.api(
                f"/repos/{repo}/git/blobs",
                "POST",
                {"content": content, "encoding": "utf-8"},
            )
            entries.append(dict(path=path, mode="100644", type="blob", sha=blob["sha"]))
        tree = self.api(
            f"/repos/{repo}/git/trees",
            "POST",
            {"base_tree": head["tree"], "tree": entries},
        )
        commit = self.api(
            f"/repos/{repo}/git/commits",
            "POST",
            {"message": message, "tree": tree["sha"], "parents": [head["sha"]]},
        )
        heartbeat()
        self.api(
            f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(branch,safe='')}",
            "PATCH",
            {"sha": commit["sha"], "force": False},
        )
        return commit["sha"]


def installation_client(installation, repository_id):
    if not configured():
        raise GitHubError("The server GitHub App is not configured", retry_after=3600)
    try:
        key = Path(settings.GITHUB_PRIVATE_KEY_FILE).read_bytes()
        now = int(time.time())
        token = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": str(settings.GITHUB_APP_ID)},
            key,
            algorithm="RS256",
        )
    except (OSError, ValueError, jwt.PyJWTError):
        raise GitHubError(
            "Cannot read or sign with the server GitHub App key"
        ) from None
    response = GitHubClient(token).api(
        f"/app/installations/{installation}/access_tokens",
        "POST",
        {"repository_ids": [repository_id], "permissions": {"contents": "write"}},
    )
    return GitHubClient(response["token"])
