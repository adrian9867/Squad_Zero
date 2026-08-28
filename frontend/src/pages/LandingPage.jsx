import React from 'react';
import { Mail } from 'lucide-react';
import { Link } from 'react-router-dom';

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto max-w-screen-2xl px-6 py-8 lg:flex lg:min-h-screen lg:items-stretch lg:gap-10 xl:gap-12">
        {/* Left side */}
        <div className="lg:flex lg:w-[52%] lg:flex-col lg:justify-between lg:pl-6 xl:pl-10">
          <div className="flex items-center gap-3">
            <img src="/logo.png" alt="Neura Note" className="h-10 w-10" />
            <span className="text-lg font-semibold text-slate-800">Neura Note</span>
          </div>

          <h1 className="mt-10 font-display text-5xl leading-tight text-slate-900 sm:text-8xl">
            Welcome to
            <br />
            Neura Note
          </h1>

          <p className="mt-6 max-w-xl text-lg text-slate-600">
            Your path to structured, continuous learning. Stop cramming, start building lasting
            knowledge.
          </p>

          <div className="mt-8 flex flex-wrap gap-3">
            {['keep Learning', 'stay update', 'grow up'].map((label) => (
              <span
                key={label}
                className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-4 py-2 text-sm text-slate-700"
              >
                <span className="h-2 w-2 rounded-full bg-indigo-500" />
                {label}
              </span>
            ))}
          </div>

          <div className="mt-12 flex items-center gap-3 text-slate-500">
            <Mail size={18} />
            <span className="text-sm">Neura Team</span>
          </div>
        </div>

        {/* Right side */}
        <div className="mt-12 lg:mt-0 lg:flex lg:w-[48%]">
          <div className="relative flex h-full min-h-[520px] w-full flex-col overflow-hidden rounded-3xl bg-gradient-to-br from-slate-900 via-indigo-950 to-slate-900 p-10 shadow-2xl">
            <div className="absolute inset-0 opacity-20">
              <div className="absolute -top-24 -right-20 h-72 w-72 rounded-full bg-indigo-500 blur-3xl" />
              <div className="absolute -bottom-28 -left-20 h-72 w-72 rounded-full bg-blue-600 blur-3xl" />
            </div>

            <div className="relative flex flex-1 items-center justify-center">
              <img
                src="/logo.png"
                alt="Neura Note Brain"
                className="w-full max-w-md object-contain"
                onError={(e) => {
                  e.currentTarget.src = '/logo.png';
                }}
              />
            </div>

            <Link
              to="/login"
              className="absolute inset-x-8 bottom-8 rounded-xl bg-slate-200/90 px-6 py-3 text-center text-sm font-semibold text-slate-800 transition hover:bg-white"
            >
              Get Started - Sign In
              <span className="block text-xs font-medium text-slate-500">Free</span>
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
