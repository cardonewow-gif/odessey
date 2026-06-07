// @ts-check
'use strict';

// Shared bot-comment helpers. A single marker-tagged comment is kept in sync:
// created when there's something to say, updated in place, and deleted when the
// problem clears. The marker is unique per check, so matching on it is enough.

/**
 * Find this check's existing bot comment (by marker), paginating so it isn't
 * missed on issues/PRs with many comments.
 * @returns {Promise<{ id: number } | undefined>}
 */
async function findComment(github, { owner, repo }, issueNumber, marker) {
  const comments = await github.paginate(github.rest.issues.listComments, {
    owner, repo, issue_number: issueNumber, per_page: 100,
  });
  return comments.find(c => (c.body ?? '').includes(marker));
}

/** Create the marker comment, or update it in place if it already exists. */
async function upsertComment(github, { owner, repo }, issueNumber, marker, body) {
  const existing = await findComment(github, { owner, repo }, issueNumber, marker);
  if (existing) {
    await github.rest.issues.updateComment({ owner, repo, comment_id: existing.id, body });
  } else {
    await github.rest.issues.createComment({ owner, repo, issue_number: issueNumber, body });
  }
}

/** Delete the marker comment if present; no-op otherwise. */
async function deleteComment(github, { owner, repo }, issueNumber, marker) {
  const existing = await findComment(github, { owner, repo }, issueNumber, marker);
  if (existing) {
    await github.rest.issues.deleteComment({ owner, repo, comment_id: existing.id });
  }
}

module.exports = { findComment, upsertComment, deleteComment };
