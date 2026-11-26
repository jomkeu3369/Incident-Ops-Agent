package com.incidentops.backend.config;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ReadListener;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
public class InternalTokenFilter extends OncePerRequestFilter {

    static final String HEADER_NAME = "X-Internal-Token";
    private static final Set<String> READ_ONLY_MCP_TOOLS =
            Set.of("searchCorpusIndex", "pullCorpusChunk", "getIncidentDetail");
    private static final Set<String> KNOWN_ROLES = Set.of("supervisor", InternalRoleContext.ROLE_INVESTIGATOR);
    // 중복 키와 뒤따르는 문서처럼 해석이 갈릴 수 있는 본문은 파싱 단계에서 거부한다
    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper()
            .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);

    private final String expectedToken;
    private final String investigatorToken;

    public InternalTokenFilter(String expectedToken) {
        this(expectedToken, null);
    }

    @Autowired
    public InternalTokenFilter(
            @Value("${app.internal-service-token:}") String expectedToken,
            @Value("${app.internal-investigator-token:}") String investigatorToken) {
        this.expectedToken = expectedToken == null ? "" : expectedToken;
        this.investigatorToken = investigatorToken;
        if (investigatorToken != null && !investigatorToken.equals(investigatorToken.strip())) {
            // 에이전트는 앞뒤 공백을 제거해 보내므로, 공백이 섞인 설정은 기동 시점에 알린다
            throw new IllegalStateException("INTERNAL_INVESTIGATOR_TOKEN의 앞뒤 공백을 제거해야 합니다.");
        }
        if (constantTimeEquals(this.expectedToken, investigatorToken)) {
            // 같은 값이면 모든 요청이 읽기 전용이 되므로 기동 시점에 설정 오류를 알린다
            throw new IllegalStateException("INTERNAL_INVESTIGATOR_TOKEN은 INTERNAL_SERVICE_TOKEN과 달라야 합니다.");
        }
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getServletPath();
        return path.equals("/actuator/health") || path.startsWith("/actuator/health/")
                || path.equals("/swagger-ui.html") || path.startsWith("/swagger-ui/")
                || path.equals("/v3/api-docs") || path.startsWith("/v3/api-docs/")
                || path.equals("/v3/api-docs.yaml");
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain) throws ServletException, IOException {

        String providedToken = request.getHeader(HEADER_NAME);
        // 공유 토큰이 없는 설정에서는 읽기 토큰도 받지 않는다(fail-closed)
        boolean dedicatedInvestigator = !expectedToken.isBlank() && constantTimeEquals(investigatorToken, providedToken);

        if (!dedicatedInvestigator && !constantTimeEquals(expectedToken, providedToken)) {
            writeJsonError(response, HttpServletResponse.SC_UNAUTHORIZED, "Unauthorized", "유효하지 않은 내부 서비스 토큰입니다.");
            return;
        }

        String role = null;
        if (dedicatedInvestigator) {
            role = InternalRoleContext.ROLE_INVESTIGATOR;
        } else {
            var roles = Collections.list(request.getHeaders(InternalRoleContext.ROLE_HEADER));
            if (!roles.isEmpty()) {
                role = roles.getFirst().strip().toLowerCase(Locale.ROOT);
                if (roles.size() != 1 || !KNOWN_ROLES.contains(role)) {
                    writeJsonError(response, HttpServletResponse.SC_FORBIDDEN, "Forbidden", "알 수 없는 내부 에이전트 역할입니다.");
                    return;
                }
            }
        }
        InternalRoleContext.setRole(role);
        try {
            String path = request.getServletPath();
            String method = request.getMethod();
            if (InternalRoleContext.isReadOnlyInvestigator()) {
                if (path.startsWith("/api/")
                        && !("GET".equalsIgnoreCase(method) || "HEAD".equalsIgnoreCase(method) || "OPTIONS".equalsIgnoreCase(method))) {
                    writeJsonError(response, HttpServletResponse.SC_FORBIDDEN, "Forbidden", "읽기 전용 조사 워커(investigator)는 쓰기 API를 호출할 수 없습니다.");
                    return;
                }
                // MCP 도구 콜백은 Reactor boundedElastic 스레드에서 실행되므로, 서블릿 경계에서 JSON-RPC 요청 본문을 직접 검사해 쓰기 도구를 선제 차단한다
                if ((path.equals("/mcp") || path.startsWith("/mcp/")) && "POST".equalsIgnoreCase(method)) {
                    byte[] bodyBytes = request.getInputStream().readAllBytes();
                    if (!isUtf8Json(request) || !isAllowedForInvestigatorMcp(bodyBytes)) {
                        writeJsonError(response, HttpServletResponse.SC_FORBIDDEN, "Forbidden", "읽기 전용 조사 워커(investigator)는 쓰기 MCP 도구를 호출할 수 없습니다.");
                        return;
                    }
                    filterChain.doFilter(new CachedBodyRequestWrapper(request, bodyBytes), response);
                    return;
                }
            }
            filterChain.doFilter(request, response);
        } finally {
            InternalRoleContext.clear();
        }
    }

    /**
     * MCP 전송 계층은 요청이 지정한 charset으로 본문을 문자열로 바꾼다. 필터가 검사한 문자열과
     * 실행되는 문자열이 같도록 UTF-8 JSON 단일 Content-Type만 허용한다.
     */
    private static boolean isUtf8Json(HttpServletRequest request) {
        List<String> contentTypes = Collections.list(request.getHeaders("Content-Type"));
        if (contentTypes.size() != 1) {
            return false;
        }
        String contentType = contentTypes.get(0);
        try {
            MediaType mediaType = MediaType.parseMediaType(contentType);
            boolean singleCharset = contentType.toLowerCase(Locale.ROOT).split("charset", -1).length <= 2;
            return singleCharset
                    && MediaType.APPLICATION_JSON.equalsTypeAndSubtype(mediaType)
                    && (mediaType.getCharset() == null || StandardCharsets.UTF_8.equals(mediaType.getCharset()));
        } catch (IllegalArgumentException ex) {
            return false;
        }
    }

    private static boolean isAllowedForInvestigatorMcp(byte[] bodyBytes) {
        if (bodyBytes == null || bodyBytes.length == 0) {
            return true;
        }
        try {
            JsonNode root = OBJECT_MAPPER.readTree(new String(bodyBytes, StandardCharsets.UTF_8));
            if (root == null || root.isMissingNode()) {
                return true;
            }
            if (root.isArray()) {
                for (JsonNode item : root) {
                    if (!isSingleMcpMessageAllowed(item)) {
                        return false;
                    }
                }
                return true;
            }
            return isSingleMcpMessageAllowed(root);
        } catch (IOException ex) {
            // 조사 워커의 파싱 불가 MCP POST 본문은 fail-closed로 거부한다
            return false;
        }
    }

    private static boolean isSingleMcpMessageAllowed(JsonNode node) {
        String rpcMethod = node.path("method").asText("");
        if (!"tools/call".equals(rpcMethod)) {
            return true;
        }
        String toolName = node.path("params").path("name").asText("");
        return READ_ONLY_MCP_TOOLS.contains(toolName);
    }

    private static void writeJsonError(HttpServletResponse response, int status, String error, String message) throws IOException {
        response.setStatus(status);
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding("UTF-8");
        response.setHeader("X-Content-Type-Options", "nosniff");
        response.setHeader("Cache-Control", "no-store");
        response.getWriter().write("{\"status\":" + status + ",\"error\":\"" + error + "\",\"message\":\"" + message + "\"}");
    }

    static boolean constantTimeEquals(String expected, String provided) {
        if (expected == null || expected.isBlank() || provided == null) {
            return false;
        }
        return MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.UTF_8),
                provided.getBytes(StandardCharsets.UTF_8));
    }

    private static final class CachedBodyRequestWrapper extends HttpServletRequestWrapper {
        private final byte[] body;

        CachedBodyRequestWrapper(HttpServletRequest request, byte[] body) {
            super(request);
            this.body = body;
        }

        @Override
        public String getCharacterEncoding() {
            return StandardCharsets.UTF_8.name();
        }

        @Override
        public ServletInputStream getInputStream() {
            ByteArrayInputStream bais = new ByteArrayInputStream(body);
            return new ServletInputStream() {
                @Override
                public boolean isFinished() {
                    return bais.available() == 0;
                }

                @Override
                public boolean isReady() {
                    return true;
                }

                @Override
                public void setReadListener(ReadListener readListener) {}

                @Override
                public int read() {
                    return bais.read();
                }
            };
        }

        @Override
        public BufferedReader getReader() {
            return new BufferedReader(new InputStreamReader(getInputStream(), StandardCharsets.UTF_8));
        }
    }
}
