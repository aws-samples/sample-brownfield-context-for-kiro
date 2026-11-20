package x;

import java.util.List;

public class GuardedApiImpl extends BaseService implements GuardedApi {

	@Override
	public Guarded saveGuarded(Guarded guarded) {
		String text = "not a brace } here";
		if (guarded == null) {
			throw new IllegalArgumentException("{");
		}
		return guarded;
	}

	@Override
	public List<Guarded> getGuarded(String name) {
		return null; // } in a comment
	}

	@Override
	public Guarded openGuarded(String id) {
		return null;
	}
}
