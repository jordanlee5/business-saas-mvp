declare const wx: {
  getAccountInfoSync(): { miniProgram: { envVersion: string } };
  getSystemInfoSync(): { platform: string };
  login(options: {
    success(result: { code: string }): void;
    fail(error: unknown): void;
  }): void;
  request(options: {
    url: string;
    method: 'GET' | 'POST';
    timeout: number;
    header: { Accept: string; 'Content-Type'?: string; Authorization?: string };
    data?: { code: string };
    success(result: { statusCode: number; data: unknown }): void;
    fail(error: unknown): void;
  }): void;
  stopPullDownRefresh(): void;
  navigateTo(options: { url: string }): void;
  navigateBack(options: { delta: number }): void;
  reLaunch(options: { url: string }): void;
};

declare function App(options: object): void;
declare function Page(options: object): void;
declare function getCurrentPages(): object[];
