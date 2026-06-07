// @ts-check
'use strict';

// Shared markdown helpers for the description/title checks.

/** Escape regex metacharacters so a heading can be interpolated literally. */
function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Strip HTML comments (template placeholders) and trim, so they don't count as content. */
function strip(text) {
  return (text ?? '').replace(/<!--[\s\S]*?-->/g, '').trim();
}

/**
 * Extract the text content of a section by heading. Matches any heading depth
 * (#, ##, ###, …) so a body isn't penalised for using a different number of
 * hashes than the template generates. The heading is matched literally — regex
 * metacharacters (e.g. the `?` in "Are you willing to implement this?") are
 * escaped internally, so callers pass the plain heading text.
 *
 * @param {string} body - the full markdown body
 * @param {string} heading - the section heading text (plain, not regex-escaped)
 * @returns {string} comment-stripped, trimmed section content ('' if not found)
 */
function section(body, heading) {
  const re = new RegExp(`#+\\s+${escapeRegExp(heading)}\\s*([\\s\\S]*?)(?=\\n#+\\s+|$)`, 'i');
  const m = (body ?? '').match(re);
  return strip(m ? m[1] : '');
}

module.exports = { escapeRegExp, strip, section };
