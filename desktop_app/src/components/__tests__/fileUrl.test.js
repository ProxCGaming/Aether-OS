import { toFileUrl } from '../renderers/fileUrl';

// FUP-2: the file-backed static preview loads the artifact's real
// file through a webview guest, so the file_path -> file:// URL
// conversion has to be exact for every path shape the artifact
// store produces (absolute OS paths from Python `str(Path)`).
describe('toFileUrl (FUP-2)', () => {
  test('POSIX absolute path', () => {
    expect(toFileUrl('/home/user/.aether/artifacts/t1/code/index.html'))
      .toBe('file:///home/user/.aether/artifacts/t1/code/index.html');
  });

  test('segments are percent-encoded, separators preserved', () => {
    expect(toFileUrl('/site/my folder/page #1?.html'))
      .toBe('file:///site/my%20folder/page%20%231%3F.html');
  });

  test('Windows drive path with backslashes', () => {
    expect(toFileUrl('C:\\Users\\arindam\\site\\index page.html'))
      .toBe('file:///C:/Users/arindam/site/index%20page.html');
  });

  test('UNC path keeps the host as the URL authority', () => {
    expect(toFileUrl('\\\\server\\share\\dir\\file.html'))
      .toBe('file://server/share/dir/file.html');
  });

  test('unusable paths return null so the caller can fall back', () => {
    expect(toFileUrl(null)).toBeNull();
    expect(toFileUrl(undefined)).toBeNull();
    expect(toFileUrl('')).toBeNull();
    expect(toFileUrl('   ')).toBeNull();
    expect(toFileUrl(42)).toBeNull();
  });
});
