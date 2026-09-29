/**
 * 应用骨架：路由表。
 *
 * 为什么从「单页 + 抽屉」改成路由：后台的两个入口原先都是聊天页页头上的按钮，
 * 抽屉一开，客服的工单台和管理者的经营看板就直接铺在用户眼前。那不是样式问题，
 * 是**权限问题**——一个功能如果想藏起来才能不出事，那它藏不住的时候就会出事。
 * 换成独立路由之后，入口本身可以被守卫，用户端页面上不再有它们的位置。
 *
 * `/chat` 而不是 `/`：用户端是默认页，但把根路径重定向过去，是为了让
 * 「/ 是什么」有一个明确的写法，将来加营销页/落地页时不必挪动用户端。
 */

import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import { AuthProvider } from './auth/AuthContext';
import RequireRole from './components/RequireRole';
import AdminPage from './pages/AdminPage';
import ChatPage from './pages/ChatPage';
import DeskPage from './pages/DeskPage';
import LoginPage from './pages/LoginPage';

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* 用户端：公开，谁都进得来 */}
          <Route path="/chat" element={<ChatPage />} />
          {/* 登录页本身不能挡：挡了就没有地方能拿到令牌 */}
          <Route path="/login" element={<LoginPage />} />
          {/* 客服与管理员都能进：工单台是客服的本职，管理员顺带能看 */}
          <Route
            path="/desk"
            element={
              <RequireRole roles={['agent', 'admin']}>
                <DeskPage />
              </RequireRole>
            }
          />
          {/* 经营看板只给管理员 */}
          <Route
            path="/admin"
            element={
              <RequireRole roles={['admin']}>
                <AdminPage />
              </RequireRole>
            }
          />
          <Route path="/" element={<Navigate to="/chat" replace />} />
          {/* 其余路径一律回用户端。nginx 对未匹配的静态资源回落到 index.html，
              所以手打一个错地址不会 404，而是落到这里 */}
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
