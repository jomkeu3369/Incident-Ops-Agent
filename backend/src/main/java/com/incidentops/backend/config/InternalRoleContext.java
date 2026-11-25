package com.incidentops.backend.config;

import java.util.Locale;

/**
 * 요청 스레드 내에서 내부 에이전트 역할(X-Agent-Role)을 보관해
 * 조사 워커(investigator)의 쓰기/완료 호출을 차단한다.
 */
public final class InternalRoleContext {

    public static final String ROLE_HEADER = "X-Agent-Role";
    public static final String ROLE_INVESTIGATOR = "investigator";

    private static final ThreadLocal<String> CURRENT_ROLE = new ThreadLocal<>();

    private InternalRoleContext() {}

    public static void setRole(String rawRole) {
        if (rawRole == null || rawRole.isBlank()) {
            CURRENT_ROLE.remove();
            return;
        }
        CURRENT_ROLE.set(rawRole.trim().toLowerCase(Locale.ROOT));
    }

    public static String getRole() {
        return CURRENT_ROLE.get();
    }

    public static boolean isReadOnlyInvestigator() {
        return ROLE_INVESTIGATOR.equals(CURRENT_ROLE.get());
    }

    public static void clear() {
        CURRENT_ROLE.remove();
    }
}
