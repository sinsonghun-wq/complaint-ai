-- ComplaintAI: 9개 부서/분류 체계에서 7개 체계로 전환한다.
-- 실행 전 백업을 권장하며, 각 UPDATE와 계정 재생성은 하나의 트랜잭션으로 처리한다.
BEGIN;

-- 기존 분류를 새 분류로 통합한다. 독립 법률 분류는 더 이상 사용하지 않는다.
UPDATE complaints
SET category = CASE category
  WHEN '행정·안전·생활서비스' THEN '행정·안전'
  WHEN '교통·주차' THEN '국토·교통'
  WHEN '도로·시설물' THEN '국토·교통'
  WHEN '법률' THEN '기타'
  ELSE category
END,
department = CASE category
  WHEN '행정·안전·생활서비스' THEN '행정·안전'
  WHEN '교통·주차' THEN '국토·교통'
  WHEN '도로·시설물' THEN '국토·교통'
  WHEN '법률' THEN '기타'
  ELSE category
END
WHERE category IN ('행정·안전·생활서비스', '교통·주차', '도로·시설물', '법률');

-- 기존 관리자와 연결된 부서별 문서/응답도 새 부서명으로 옮긴다.
UPDATE department_documents
SET department = CASE department
  WHEN '행정·안전·생활서비스' THEN '행정·안전'
  WHEN '교통·주차' THEN '국토·교통'
  WHEN '도로·시설물' THEN '국토·교통'
  WHEN '법률' THEN '기타'
  ELSE department
END
WHERE department IN ('행정·안전·생활서비스', '교통·주차', '도로·시설물', '법률');

UPDATE complaint_responses
SET department = CASE department
  WHEN '행정·안전·생활서비스' THEN '행정·안전'
  WHEN '교통·주차' THEN '국토·교통'
  WHEN '도로·시설물' THEN '국토·교통'
  WHEN '법률' THEN '기타'
  ELSE department
END
WHERE department IN ('행정·안전·생활서비스', '교통·주차', '도로·시설물', '법률');

COMMIT;
