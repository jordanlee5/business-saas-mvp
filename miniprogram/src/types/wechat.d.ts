declare const wx: {
  getAccountInfoSync(): { miniProgram: { appId: string; envVersion: string } };
  getSystemInfoSync(): { platform: string };
  getStorageSync(key: string): unknown;
  setStorageSync(key: string, data: unknown): void;
  getPrivacySetting(options: {
    success(result: { needAuthorization: boolean; privacyContractName: string }): void;
    fail(error: unknown): void;
  }): void;
  openPrivacyContract(options: {
    success(): void;
    fail(error: unknown): void;
  }): void;
  login(options: {
    success(result: { code: string }): void;
    fail(error: unknown): void;
  }): void;
  request(options: {
    url: string;
    method: 'GET' | 'POST' | 'PUT' | 'DELETE';
    timeout: number;
    header: { Accept: string; 'Content-Type'?: string; Authorization?: string };
    data?: Record<string, unknown> | {
      wechat_code: string;
      business_public_no: string;
      phone: string;
      plate_number: string;
      activation_code: string;
    };
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
