module.exports = {
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/src/setupTests.js'],
  moduleNameMapper: {
    '\\.(css|less|scss|sass)$': '<rootDir>/__mocks__/styleMock.js',
    '\\.(gif|ttf|eot|svg|png)$': '<rootDir>/__mocks__/fileMock.js'
  },
  transform: {
    '^.+\\.jsx?$': 'babel-jest',
  },
  // Coverage (S5-M11): measurement only — no coverageThreshold, so a
  // coverage run reports but never fails on a percentage. Run it with
  // `npm run test:coverage` (see README "Test" section).
  collectCoverageFrom: [
    'src/**/*.{js,jsx}',
    '!src/__tests__/**',
    '!src/setupTests.js',
  ],
  coverageDirectory: 'coverage',
  coverageReporters: ['text', 'lcov', 'html'],
};
