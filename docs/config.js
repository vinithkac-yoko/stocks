// Leave the Supabase fields empty to run in "this device only" mode.
// Fill all three to sync across phone and laptop (see README, step 3).
// The anon key is meant to be public; row level security keeps your data private.
window.APP_CONFIG = {
  batchSize: 10,          // stocks you review per day
  supabaseUrl: '',        // e.g. https://abcdxyz.supabase.co
  supabaseAnonKey: '',    // Project settings -> API -> anon public key
  loginEmail: '',         // the one user you create in Supabase; the passcode is its password
};
