import { describe, expect, it } from 'vitest';
import {
  requireSafeUiTarget,
  requireSafeUiUrl,
} from '../../src/ui/url-policy';

describe('requireSafeUiUrl', () => {
  it.each([
    ['https://example.test/path?query=value#section', 'https://example.test/path?query=value#section'],
    ['HTTP://EXAMPLE.TEST:80/path', 'http://example.test/path'],
    ['https://[::1]:8443/image.png', 'https://[::1]:8443/image.png'],
  ])('accepts and canonicalizes explicit HTTP(S) URL %s', (input, expected) => {
    expect(requireSafeUiUrl(input, 'test.href')).toBe(expected);
  });

  it.each([
    '',
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'data:image/svg+xml,<svg onload=alert(1)>',
    'blob:https://example.test/id',
    'file:///etc/passwd',
    'mailto:user@example.test',
    '//evil.example/path',
    '/relative/path',
    'relative/path',
    '#fragment',
    'https:\\evil.example/path',
    'https:/evil.example/path',
    'http:evil.example/path',
    'https://user:password@example.test/path',
    ' https://example.test/path',
    'https://example.test/path with space',
    'java\nscript:alert(1)',
    'jav\u0000ascript:alert(1)',
    'javascript&#58;alert(1)',
    `https://example.test/${'x'.repeat(4096)}`,
  ])('rejects unsafe or ambiguous URL %j', (input) => {
    expect(() => requireSafeUiUrl(input, 'test.href'))
      .toThrow('test.href must be an absolute HTTP(S) URL');
  });
});

describe('requireSafeUiTarget', () => {
  it.each(['_self', '_blank'])('accepts target %s', (target) => {
    expect(requireSafeUiTarget(target, 'test.target')).toBe(target);
  });

  it.each(['_parent', '_top', 'named-frame', 'javascript:alert(1)', ''])(
    'rejects target %j',
    (target) => {
      expect(() => requireSafeUiTarget(target, 'test.target'))
        .toThrow('test.target must be one of: _self, _blank');
    }
  );
});
