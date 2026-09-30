// 설치(홈 화면 추가)용 최소 서비스 워커. 캐시하지 않고 그대로 네트워크로 보낸다 —
// 학습 기록이 서버 DB에 있어서 오프라인 모드는 의미가 없고, 캐시는 배포 후 옛 화면이
// 남는 문제만 만든다.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
