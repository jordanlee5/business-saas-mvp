declare const wx: {
  getAccountInfoSync(): { miniProgram: { envVersion: string } };
  getSystemInfoSync(): { platform: string };
  request(options: {
    url: string;
    method: 'GET';
    timeout: number;
    header: { Accept: string };
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
