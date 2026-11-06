module.exports = {
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/src/setupTests.js'],
  moduleNameMapper: {
    '\\.(css|less|scss|sass)$': '<rootDir>/__mocks__/styleMock.js',
    '\\.(gif|ttf|eot|svg|png)$': '<rootDir>/__mocks__/fileMock.js',
    // remark-gfm is ESM-only; Jest (CJS) loads this stub instead.
    // See __mocks__/remarkGfmMock.js for the verification story.
    '^remark-gfm$': '<rootDir>/__mocks__/remarkGfmMock.js'
  },
  transform: {
    '^.+\\.jsx?$': 'babel-jest',
  },
  // Coverage (S5-M11): measurement only — no coverageThreshold, so a
  // coverage run reports but never fails on a percentage. Run it with
  // `npm run test:coverage` (see README "Test" section).
  // D4-13: the main-process layer is measured too — electron/*.cjs
  // and the root main.cjs / preload.cjs are the most privileged code
  // in the app and were exactly what the old src/**-only set never
  // reported on.
  collectCoverageFrom: [
    'src/**/*.{js,jsx}',
    '!src/__tests__/**',
    '!src/setupTests.js',
    'electron/**/*.cjs',
    'main.cjs',
    'preload.cjs',
    '!electron/__tests__/**',
  ],
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov', 'html'],
};
