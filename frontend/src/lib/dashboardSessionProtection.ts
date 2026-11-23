import {
  SessionProtection,
  type SessionProtectionStore,
  SessionProtectionUnavailable,
} from "../../../sdk/typescript/src/session-protection.js";

const DATABASE = "loom-dashboard-session-protection";
const STORE = "authority";
const KEY = "dashboard";
const WITNESS = "loom-dashboard-session-protection-v1";

interface Authority {
  version: 1;
  family: string;
  state: "protected";
}

function authority(value: unknown, family: string): value is Authority {
  if (value === null || typeof value !== "object") return false;
  const row = value as Partial<Authority>;
  return Object.keys(value).length === 3 && row.version === 1 && row.family === family && row.state === "protected";
}

/** One content-free family for every dashboard namespace, tab and restored conversation. */
export class DashboardSessionProtectionStore implements SessionProtectionStore {
  private readonly database: IDBDatabase;
  private readonly storage: Storage;
  private readonly family: string;

  private constructor(
    database: IDBDatabase,
    storage: Storage,
    family: string,
  ) {
    this.database = database;
    this.storage = storage;
    this.family = family;
  }

  static open(): Promise<DashboardSessionProtectionStore> {
    return new Promise((resolve, reject) => {
      let storage: Storage;
      let witness: string | null;
      let opening: IDBOpenDBRequest;
      let family: string | undefined;
      let refused = false;
      const timer = setTimeout(() => refuse(), 60_000);
      const refuse = () => {
        refused = true;
        clearTimeout(timer);
        reject(new SessionProtectionUnavailable());
      };
      try {
        storage = window.localStorage;
        witness = storage.getItem(WITNESS);
        opening = window.indexedDB.open(DATABASE, 1);
      } catch {
        refuse();
        return;
      }
      opening.onblocked = refuse;
      opening.onerror = refuse;
      opening.onupgradeneeded = (event) => {
        try {
          // A surviving witness means this is lost proof, not a first installation.
          if (refused || witness !== null || event.oldVersion !== 0) {
            opening.transaction?.abort();
            refuse();
            return;
          }
          family = window.crypto.randomUUID();
          const store = opening.result.createObjectStore(STORE);
          // No blank browser or complete storage wipe can manufacture a clean family.
          store.add({ version: 1, family, state: "protected" } satisfies Authority, KEY);
          storage.setItem(WITNESS, family);
        } catch {
          opening.transaction?.abort();
          refuse();
        }
      };
      opening.onsuccess = () => {
        const database = opening.result;
        if (refused) {
          database.close();
          return;
        }
        database.onversionchange = () => database.close();
        try {
          // Another tab may have provisioned while this open waited for its upgrade.
          family ??= storage.getItem(WITNESS) ?? undefined;
        } catch {
          database.close();
          refuse();
          return;
        }
        if (!family) {
          database.close();
          refuse();
          return;
        }
        const store = new DashboardSessionProtectionStore(database, storage, family);
        store.mark().then(
          () => {
            if (refused) database.close();
            else {
              clearTimeout(timer);
              resolve(store);
            }
          },
          () => {
            database.close();
            refuse();
          },
        );
      };
    });
  }

  private transaction(mark: boolean): Promise<"protected"> {
    return new Promise((resolve, reject) => {
      let transaction: IDBTransaction;
      let valid = false;
      const refuse = () => reject(new SessionProtectionUnavailable());
      try {
        if (this.storage.getItem(WITNESS) !== this.family) throw new SessionProtectionUnavailable();
        transaction = this.database.transaction(STORE, mark ? "readwrite" : "readonly", {
          durability: "strict",
        });
        const store = transaction.objectStore(STORE);
        const request = store.get(KEY);
        request.onsuccess = () => {
          try {
            if (this.storage.getItem(WITNESS) !== this.family || !authority(request.result, this.family)) {
              transaction.abort();
              return;
            }
            valid = true;
            // Never repair a missing row and never write clean, even after a storage reset.
            if (mark) store.put({ version: 1, family: this.family, state: "protected" } satisfies Authority, KEY);
          } catch {
            transaction.abort();
          }
        };
        transaction.oncomplete = () => {
          try {
            if (!valid || this.storage.getItem(WITNESS) !== this.family) throw new SessionProtectionUnavailable();
            resolve("protected");
          } catch {
            refuse();
          }
        };
        transaction.onabort = refuse;
        transaction.onerror = refuse;
      } catch {
        refuse();
      }
    });
  }

  read(): Promise<"protected"> {
    return this.transaction(false);
  }

  async mark(): Promise<void> {
    await this.transaction(true);
  }
}

let retained: Promise<SessionProtection> | undefined;

/** A rejected restore stays rejected; requests cannot provision a replacement family. */
export function dashboardSessionProtection(): Promise<SessionProtection> {
  retained ??= DashboardSessionProtectionStore.open().then((store) => new SessionProtection(store));
  return retained;
}
