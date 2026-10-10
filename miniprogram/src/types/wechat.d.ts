declare const wx: {
  request(options: {
    url: string;
    method: 'GET';
    timeout: number;
    header: { Accept: string };
    success(result: { statusCode: number; data: unknown }): void;
    fail(error: unknown): void;
  }): void;
  stopPullDownRefresh(): void;
};

declare function App(options: object): void;
declare function Page(options: object): void;
