// @ts-check
'use strict';

// Shared label helpers. These workflows never CREATE labels — managing the
// repo's label set is the maintainer's job. We check a label exists before
// applying it (issues.addLabels would otherwise silently create a missing one)
// and fail soft — warn and skip — if it's absent.

/**
 * @param {import('@octokit/rest').Octokit} github
 * @param {{ owner: string, repo: string }} repo
 * @param {string} name
 * @returns {Promise<boolean>}
 */
async function labelExists(github, { owner, repo }, name) {
  try {
    await github.rest.issues.getLabel({ owner, repo, name });
    return true;
  } catch (e) {
    if (e.status === 404) return false;
    throw e;
  }
}

/** Add a label if it exists; otherwise warn and skip (never auto-create). */
async function addLabel(github, core, { owner, repo }, issueNumber, name) {
  if (await labelExists(github, { owner, repo }, name)) {
    await github.rest.issues.addLabels({ owner, repo, issue_number: issueNumber, labels: [name] });
  } else {
    core.warning(`Label "${name}" does not exist in the repo — skipping. Create it once to enable labelling.`);
  }
}

/** Remove a label, ignoring "not present" (404/410). */
async function dropLabel(github, { owner, repo }, issueNumber, name) {
  try {
    await github.rest.issues.removeLabel({ owner, repo, issue_number: issueNumber, name });
  } catch (e) {
    if (e.status !== 404 && e.status !== 410) throw e;
  }
}

/** Apply `add` (fail-soft) and remove `remove`. */
async function swapLabel(github, core, { owner, repo }, issueNumber, add, remove) {
  await addLabel(github, core, { owner, repo }, issueNumber, add);
  await dropLabel(github, { owner, repo }, issueNumber, remove);
}

module.exports = { labelExists, addLabel, dropLabel, swapLabel };
